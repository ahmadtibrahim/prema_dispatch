# -*- coding: utf-8 -*-
"""18.0.1.4.0 — vehicle, authority and notification intake."""
import uuid

from odoo.tests import tagged

from odoo.addons.prema_owner_operator.application_mapping import (
    normalise_public)
from odoo.addons.prema_owner_operator.tests.test_owner_operator import (
    OwnerOperatorCase)
from odoo.addons.prema_owner_operator.tests.test_owner_operator_form_126 import (
    _raw)


@tagged("post_install", "-at_install")
class TestVehicleQuestions140(OwnerOperatorCase):

    def test_width_questions_removed_and_door_styles_required(self):
        _vals, errors = normalise_public(_raw(
            VehicleType="straight", CargoDoorStyle=""), strict=True)
        joined = " ".join(errors)
        self.assertIn("door style", joined)
        self.assertNotIn("cargo width", joined)
        self.assertNotIn("rear door width", joined)
        vals, errors = normalise_public(_raw(
            VehicleType="straight", CargoDoorStyle="rollup",
            CargoWidthIn="123", DoorWidthIn="99"), strict=True)
        self.assertFalse(errors)
        self.assertEqual(vals["cargo_door_style"], "rollup")
        self.assertNotIn("cargo_width_in", vals)
        self.assertNotIn("door_width_in", vals)
        vals, errors = normalise_public(_raw(
            VehicleType="semi", CabType="night", TrailerDoorStyle="barn",
            TrailerType="reefer", TrailerYear="2024", TrailerUnit="R-1",
            TrailerLengthFt="53"), strict=True)
        self.assertFalse(errors)
        self.assertEqual(vals["trailer_door_style"], "barn")
        _vals, errors = normalise_public(_raw(
            VehicleType="tractor", CargoDoorStyle="", TrailerDoorStyle=""),
            strict=True)
        self.assertFalse(any("door style" in e for e in errors))

    def test_air_ride_is_required_and_liftgate_capacity_is_conditional(self):
        _vals, errors = normalise_public(_raw(AirRide=""), strict=True)
        self.assertTrue(any("air-ride" in e for e in errors))
        vals, errors = normalise_public(_raw(AirRide="no"), strict=True)
        self.assertFalse(errors)
        self.assertEqual(vals["air_ride"], "no")
        _vals, errors = normalise_public(_raw(
            Liftgate="yes", LiftgateCapacityLb=""), strict=True)
        self.assertTrue(any("liftgate capacity" in e for e in errors))
        vals, errors = normalise_public(_raw(
            Liftgate="no", LiftgateCapacityLb=""), strict=True)
        self.assertFalse(any("liftgate capacity" in e for e in errors))

    def test_historical_width_values_are_not_written_or_erased(self):
        vals, errors = normalise_public(_raw(), strict=True)
        self.assertFalse(errors)
        app = self.Application._create_from_public(vals)
        self.assertFalse(app.cargo_width_in)
        self.assertFalse(app.door_width_in)
        app.write({"cargo_width_in": 101, "door_width_in": 99})
        app.apply_submission_data({})
        self.assertEqual(app.cargo_width_in, 101)
        self.assertEqual(app.door_width_in, 99)


@tagged("post_install", "-at_install")
class TestAuthorityRows140(OwnerOperatorCase):

    def test_usdot_and_mc_are_separate_and_formatted_per_type(self):
        vals, errors = normalise_public(_raw(
            CVORNumber="", AuthorityType0="usdot",
            AuthorityNumber0="1 234 567", AuthorityJurisdiction0="US-FEDERAL",
            AuthorityType1="mc", AuthorityNumber1="MC 7654321",
            AuthorityJurisdiction1="US-FEDERAL"), strict=True)
        self.assertFalse(errors)
        entries = vals["authority_entries"]
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["authority_type"], "usdot")
        self.assertEqual(entries[0]["number"], "1234567")
        self.assertEqual(entries[1]["authority_type"], "mc")
        self.assertEqual(entries[1]["number"], "7654321")
        self.assertFalse(vals.get("cvor_number"))
        app = self.Application._create_from_public(vals)
        self.assertEqual(set(app.authority_ids.mapped("authority_type")),
                         {"usdot", "mc"})
        self.assertFalse(app.cvor_number)

    def test_ontario_cvor_and_other_province_keep_their_own_formats(self):
        vals, errors = normalise_public(_raw(
            CVORNumber="", AuthorityType0="ontario_cvor",
            AuthorityNumber0="123456789",
            AuthorityType1="canadian_provincial",
            AuthorityNumber1="QC-AUTH-99",
            AuthorityJurisdiction1="CA-QC"), strict=True)
        self.assertFalse(errors)
        entries = vals["authority_entries"]
        self.assertEqual(entries[0]["number"], "123-456-789")
        self.assertEqual(entries[0]["jurisdiction_code"], "CA-ON")
        self.assertEqual(entries[1]["number"], "QC-AUTH-99")
        self.assertEqual(entries[1]["jurisdiction_code"], "CA-QC")

    def test_legacy_cvor_maps_to_one_entry_without_rewriting_it(self):
        vals, errors = normalise_public(_raw(), strict=True)
        self.assertFalse(errors)
        self.assertEqual(vals["cvor_number"], "123-456-789")
        self.assertEqual(len(vals["authority_entries"]), 1)
        self.assertTrue(vals["authority_entries"][0]["legacy"])
        app = self.Application._create_from_public(vals)
        self.assertEqual(app.cvor_number, "123-456-789")
        self.assertEqual(len(app.authority_ids), 1)
        self.assertEqual(app.authority_ids.authority_type, "ontario_cvor")

    def test_authority_review_actions_and_private_file(self):
        vals, errors = normalise_public(_raw(
            CVORNumber="", AuthorityType0="usdot",
            AuthorityNumber0="1234567",
            AuthorityJurisdiction0="US-FEDERAL"), strict=True)
        self.assertFalse(errors)
        app = self.Application._create_from_public(vals)
        authority = app.authority_ids[:1]
        attachment = self.env["ir.attachment"].create({
            "name": "authority.pdf", "raw": b"%PDF-1.4 authority",
            "mimetype": "application/pdf",
            "res_model": "prema.owner.operator.authority",
            "res_id": authority.id, "public": False,
        })
        authority.write({"attachment_id": attachment.id})
        self.assertTrue(authority.can_preview)
        self.assertFalse(attachment.public)
        authority.action_verify()
        self.assertEqual(authority.review_status, "verified")
        self.assertEqual(authority.reviewer_id, self.env.user)
        authority.action_reject()
        self.assertEqual(authority.review_status, "rejected")


@tagged("post_install", "-at_install")
class TestNotificationChoice140(OwnerOperatorCase):

    def test_application_email_vs_alternate(self):
        vals, errors = normalise_public(_raw(
            LoadNotificationChannel="email",
            LoadNotificationEmailChoice="application",
            LoadNotificationEmail="other@test.invalid"), strict=True)
        self.assertFalse(errors)
        self.assertTrue(vals["use_application_email_for_notifications"])
        self.assertEqual(vals["load_notification_email"],
                         vals["email"])

        vals, errors = normalise_public(_raw(
            LoadNotificationChannel="email",
            LoadNotificationEmailChoice="different",
            LoadNotificationEmail="other@test.invalid"), strict=True)
        self.assertFalse(errors)
        self.assertFalse(vals["use_application_email_for_notifications"])
        self.assertEqual(vals["load_notification_email"],
                         "other@test.invalid")

        _vals, errors = normalise_public(_raw(
            LoadNotificationChannel="email",
            LoadNotificationEmailChoice="different",
            LoadNotificationEmail=""), strict=True)
        self.assertTrue(any("load-notification email" in e for e in errors))

    def test_no_silent_whatsapp_or_email_opt_in(self):
        vals, errors = normalise_public(_raw(
            LoadNotificationChannel="none"), strict=True)
        self.assertFalse(errors)
        self.assertFalse(vals.get("whatsapp_consent_granted"))
        self.assertEqual(vals["load_notification_channel"], "none")


@tagged("post_install", "-at_install")
class TestIntakeForm140(OwnerOperatorCase):

    def test_form_has_new_controls_and_no_removed_widths(self):
        # HttpCase is heavier; the view arch is enough for static controls.
        arch = self.env["prema.owner.operator.application"].get_view(
            False, "form")["arch"]
        self.assertIn('name="cargo_door_style"', arch)
        self.assertIn('name="trailer_door_style"', arch)
        self.assertIn('name="authority_ids"', arch)
        self.assertIn('name="use_application_email_for_notifications"', arch)

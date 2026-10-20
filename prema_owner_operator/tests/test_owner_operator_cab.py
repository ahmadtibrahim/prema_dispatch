# -*- coding: utf-8 -*-
"""Cab selection: public mapping, persistence, mirror and Fleet hand-off."""
import re
import uuid

from odoo.exceptions import ValidationError
from odoo.tests import HttpCase, tagged

from odoo.addons.prema_owner_operator.application_mapping import (
    CAB_TYPES, normalise_public)
from odoo.addons.prema_owner_operator.tests.test_owner_operator import (
    OwnerOperatorCase)


def _raw(**overrides):
    vals = {
        "partner_name": "Cab Driver",
        "email_from": "cab-%s@test.invalid" % uuid.uuid4().hex[:8],
        "partner_phone": "(416) 555-0199",
        "LegalBusinessName": "Cab Transport Ltd",
        "BusinessStructure": "sole",
        "HomeCity": "Mississauga",
        "HomeProvince": "ON",
        "CommercialDrivingYears": "6–10 years",
        "DriverLicenceClass": "AZ",
        "LicenceJurisdiction": "ON",
        "OwnerOperatorYears": "3–5 years",
        "LicenceSuspension": "No",
        "RecentCollisions": "None",
        "RecentConvictions": "None",
        "TDGTraining": "No",
        "VehicleType": "straight",
        "CVORNumber": "123456789",
        "TruckMake": "Freightliner",
        "TruckModel": "M2 106",
        "TruckYear": "2013",
        "UnitNumber": "CAB-1",
        "VehicleOwnership": "Own",
        "Transmission": "Automatic",
        "OdometerKm": "530,000",
        "TruckPlate": "cab 123",
        "TruckPlateProvince": "ON",
        "GVWRLb": "26,000",
        "CargoBodyType": "dry",
        "CargoDoorStyle": "rollup",
        "TrailerDoorStyle": "rollup",
        "LegalPayloadLb": "12,000",
        "PalletCapacity": "13",
        "CargoLengthFt": "26",
        "CargoWidthIn": "100",
        "CargoHeightIn": "96",
        "DoorWidthIn": "96",
        "DoorHeightIn": "87",
        "Liftgate": "no",
        "AirRide": "yes",
        "ServiceProvinceON": "Ontario",
        "WorkPreferenceRegional": "1",
        "LoadNotificationChannel": "email",
        "UseApplicationEmailForNotifications": "1",
        "ApplicationDeclaration": "Confirmed",
    }
    vals.update(overrides)
    return vals


def _semi_raw(**overrides):
    vals = _raw(**overrides)
    vals.update({
        "VehicleType": "semi",
        "TrailerType": "reefer",
        "TrailerYear": "2024",
        "TrailerUnit": "REEFER-1",
        "TrailerLengthFt": "53",
    })
    vals.update(overrides)
    return vals


@tagged("post_install", "-at_install")
class TestCabSelection(OwnerOperatorCase):

    def test_cab_choices_and_default_are_exact(self):
        self.assertEqual(CAB_TYPES, {
            "not_applicable": "Not Applicable",
            "day": "Day Cab",
            "night": "Night Cab",
        })
        vals, errors = normalise_public(_raw(), strict=True)
        self.assertFalse(errors)
        self.assertEqual(vals["cab_type"], "not_applicable")

    def test_all_vehicle_types_accept_cab(self):
        cases = (
            ("cargo_van", "day",
             _raw(VehicleType="cargo_van", CabType="day")),
            ("straight", "night",
             _raw(VehicleType="straight", CabType="night")),
            ("tractor", "day",
             _raw(VehicleType="tractor", CabType="day")),
            ("semi", "night", _semi_raw(CabType="night")),
        )
        for vehicle_type, cab, raw in cases:
            vals, errors = normalise_public(raw, strict=True)
            self.assertFalse(errors, (vehicle_type, errors))
            self.assertEqual(vals["vehicle_type"], vehicle_type)
            self.assertEqual(vals["cab_type"], cab)

    def test_tractor_and_semi_require_explicit_day_or_night(self):
        for raw in (_raw(VehicleType="tractor"), _semi_raw()):
            for missing in ("", "not_applicable"):
                vals, errors = normalise_public(
                    dict(raw, CabType=missing), strict=True)
                self.assertTrue(
                    any("Day Cab or Night Cab" in e for e in errors),
                    (raw["VehicleType"], missing, errors))
                self.assertNotIn("cab_type", vals)

    def test_van_and_straight_default_to_not_applicable(self):
        for vehicle_type in ("cargo_van", "straight"):
            vals, errors = normalise_public(
                _raw(VehicleType=vehicle_type), strict=True)
            self.assertFalse(errors)
            self.assertEqual(vals["cab_type"], "not_applicable")

    def test_type_switch_revalidates_and_clears_inapplicable_value(self):
        vals, errors = normalise_public(_semi_raw(CabType="night"),
                                        strict=True)
        self.assertFalse(errors)
        app = self.Application._create_from_public(vals)
        self.assertEqual(app.cab_type, "night")

        app.apply_submission_data({"vehicle_type": "cargo_van"})
        self.assertEqual(app.cab_type, "not_applicable")

        app.apply_submission_data(
            {"vehicle_type": "semi", "cab_type": "day"})
        self.assertEqual(app.cab_type, "day")

    def test_notification_none_is_accepted_and_logged(self):
        vals, errors = normalise_public(
            _raw(LoadNotificationChannel="none"), strict=True)
        self.assertFalse(errors)
        app = self.Application._create_from_public(vals)
        self.assertEqual(app.load_notification_channel, "none")
        self.assertEqual(
            app.notification_preference_log_ids.channel, "none")

    def test_server_rejects_missing_declaration(self):
        vals, errors = normalise_public(
            _raw(ApplicationDeclaration=""), strict=True)
        self.assertTrue(any("declaration" in e.lower() for e in errors))
        with self.assertRaises(ValidationError):
            self.Application._create_from_public({
                key: value for key, value in vals.items()
                if key != "application_declaration"})

    def test_post_back_persists_and_recruitment_mirror_reads_cab(self):
        day_vals, errors = normalise_public(
            _semi_raw(CabType="day"), strict=True)
        self.assertFalse(errors)
        day = self.Application._create_from_public(day_vals)
        self.assertEqual(day.cab_type, "day")
        self.assertEqual(day.hr_applicant_id.oo_cab_type, "day")
        self.assertEqual(
            day.hr_applicant_id.owner_operator_application_id.cab_type,
            "day")

        night_vals, errors = normalise_public(
            _semi_raw(CabType="night"), strict=True)
        self.assertFalse(errors)
        night = self.Application._create_from_public(night_vals)
        self.assertEqual(night.cab_type, "night")
        self.assertEqual(night.hr_applicant_id.oo_cab_type, "night")

    def test_older_records_stay_not_applicable_and_are_not_reclassified(self):
        legacy = self.Application._create_from_public(
            self._public_vals(vehicle_type="straight"))
        self.assertEqual(legacy.cab_type, "not_applicable")
        legacy.apply_submission_data({}, only_empty=True)
        self.assertEqual(legacy.cab_type, "not_applicable")

        semi = self.Application._create_from_public(
            self._public_vals(
                vehicle_type="semi", trailer_ownership="own",
                trailer_type="reefer", trailer_year="2021",
                trailer_unit="LEGACY", trailer_length_ft=53))
        self.assertEqual(semi.cab_type, "not_applicable")
        semi.apply_submission_data({}, only_empty=True)
        self.assertEqual(semi.cab_type, "not_applicable")

    def test_fleet_handoff_carries_cab_and_tag(self):
        vals, errors = normalise_public(_semi_raw(CabType="night"),
                                        strict=True)
        self.assertFalse(errors)
        app = self.Application._create_from_public(vals)
        driver = self.env["res.partner"].create({"name": "Cab Driver"})
        vehicle_vals = app._vehicle_vals(driver)
        self.assertEqual(vehicle_vals["oo_cab_type"], "night")
        self.assertIn("Cab: Night", app._generated_tag_names())

        van = self.Application._create_from_public(
            normalise_public(
                _raw(VehicleType="cargo_van"), strict=True)[0])
        self.assertEqual(van.cab_type, "not_applicable")
        self.assertNotIn("Cab:", " ".join(van._generated_tag_names()))


@tagged("post_install", "-at_install")
class TestPublicFormCab(HttpCase):

    def setUp(self):
        super().setUp()
        job = self.env["hr.job"].browse(5)
        if job.exists():
            job.write({"is_published": True})
        self.env["ir.config_parameter"].sudo().set_param(
            "prema_owner_operator.job_id", 5)

    def _token(self):
        page = self.url_open("/owner-operator/apply")
        self.assertEqual(page.status_code, 200)
        return re.search(
            r'name="csrf_token" value="([^"]+)"', page.text).group(1)

    def test_form_exposes_cab_select(self):
        page = self.url_open("/owner-operator/apply")
        self.assertIn('name="CabType"', page.text)
        self.assertIn(">Not Applicable</option>", page.text)
        self.assertIn(">Day Cab</option>", page.text)
        self.assertIn(">Night Cab</option>", page.text)

    def test_post_without_declaration_creates_no_application(self):
        email = "no-consent-%s@test.invalid" % uuid.uuid4().hex[:8]
        payload = _raw(email_from=email)
        payload.pop("ApplicationDeclaration")
        payload["csrf_token"] = self._token()
        response = self.url_open("/owner-operator/apply", data=payload)
        self.assertEqual(response.status_code, 200)
        self.assertIn("declaration", response.text.lower())
        self.assertFalse(
            self.env["prema.owner.operator.application"].search(
                [("email", "=", email)]))

    def test_semi_requires_cab_choice_server_side(self):
        email = "no-cab-%s@test.invalid" % uuid.uuid4().hex[:8]
        payload = _semi_raw(email_from=email)
        payload.pop("CabType", None)
        payload["csrf_token"] = self._token()
        response = self.url_open("/owner-operator/apply", data=payload)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Day Cab or Night Cab", response.text)
        self.assertFalse(
            self.env["prema.owner.operator.application"].search(
                [("email", "=", email)]))

    def test_public_post_persists_night_cab(self):
        email = "night-%s@test.invalid" % uuid.uuid4().hex[:8]
        payload = _semi_raw(email_from=email, CabType="night")
        payload["csrf_token"] = self._token()
        response = self.url_open("/owner-operator/apply", data=payload)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Thank you", response.text)
        app = self.env["prema.owner.operator.application"].search(
            [("email", "=", email)])
        self.assertEqual(len(app), 1)
        self.assertEqual(app.cab_type, "night")
        self.assertEqual(app.hr_applicant_id.oo_cab_type, "night")

# -*- coding: utf-8 -*-
"""18.0.1.2.6 — seven-section public form acceptance tests."""
import re
import uuid
from unittest.mock import patch

from odoo.tests import HttpCase, tagged

from odoo.addons.prema_owner_operator.application_mapping import (
    PROVINCE_CHOICES, PROVINCES, normalise_public)
from odoo.addons.prema_owner_operator.controllers.main import (
    OwnerOperatorController)
from odoo.addons.prema_owner_operator.tests.test_owner_operator import (
    OwnerOperatorCase)


def _raw(**overrides):
    vals = {
        "partner_name": "Form Driver",
        "email_from": "form-%s@test.invalid" % uuid.uuid4().hex[:8],
        "partner_phone": "(416) 555-3510",
        "LegalBusinessName": "Form Transport Ltd",
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
        "TruckMake": "Freightliner",
        "TruckModel": "M2 106",
        "TruckYear": "2013",
        "UnitNumber": "621",
        "VehicleOwnership": "Own",
        "Transmission": "Automatic",
        "OdometerKm": "530,000",
        "TruckPlate": "abc 123",
        "TruckPlateProvince": "ON",
        "GVWRLb": "26,000",
        "CargoBodyType": "dry",
        "LegalPayloadLb": "12,000",
        "PalletCapacity": "13",
        "CargoLengthFt": "26",
        "CargoWidthIn": "100",
        "CargoHeightIn": "96",
        "DoorWidthIn": "96",
        "DoorHeightIn": "87",
        "Liftgate": "no",
        "AirRide": "yes",
        "CVORNumber": "123456789",
        "ServiceProvinceON": "Ontario",
        "ServiceProvinceQC": "Quebec",
        "WorkPreferenceLocal": "1",
        "WorkPreferenceRegional": "1",
        "LoadNotificationChannel": "email",
        "UseApplicationEmailForNotifications": "1",
        "ApplicationDeclaration": "Confirmed",
    }
    vals.update(overrides)
    return vals


@tagged("post_install", "-at_install")
class TestFormMapping126(OwnerOperatorCase):

    def test_legal_name_and_phone_are_required_and_normalised(self):
        vals, errors = normalise_public(
            _raw(partner_name="", partner_phone=""), strict=True)
        self.assertTrue(any("full legal name" in e for e in errors))
        self.assertTrue(any("contact phone" in e.lower() for e in errors))
        vals, errors = normalise_public(_raw(), strict=True)
        self.assertFalse(errors)
        self.assertEqual(vals["phone"], "+14165553510")

    def test_whatsapp_same_as_phone_and_independent_number(self):
        same, errors = normalise_public(_raw(
            LoadNotificationChannel="whatsapp",
            WhatsAppConsentGranted="1",
            UseContactPhoneForWhatsApp="1", WhatsAppNumber=""), strict=True)
        self.assertFalse(errors)
        self.assertTrue(same["use_contact_phone_for_whatsapp"])
        self.assertEqual(same["whatsapp_number"], "+14165553510")
        other, errors = normalise_public(_raw(
            LoadNotificationChannel="whatsapp",
            WhatsAppConsentGranted="1",
            WhatsAppNumber="+1 647 555 1234"), strict=True)
        self.assertFalse(errors)
        self.assertFalse(other["use_contact_phone_for_whatsapp"])
        self.assertEqual(other["whatsapp_number"], "+16475551234")

    def test_whatsapp_channel_requires_number_and_consent(self):
        _vals, errors = normalise_public(_raw(
            LoadNotificationChannel="whatsapp",
            WhatsAppNumber="", WhatsAppConsentGranted=""), strict=True)
        self.assertTrue(any("WhatsApp number" in e for e in errors))
        self.assertTrue(any("agree to WhatsApp" in e for e in errors))

    def test_google_city_selection_stores_all_location_fields(self):
        resolved = {
            "place_id": "ChIJ_city", "city": "Mississauga",
            "province": "Ontario", "province_code": "ON",
            "latitude": 43.589, "longitude": -79.644,
            "country_code": "CA",
        }

        class FakePlaces:
            def __init__(self, env):
                pass

            def resolve_place(self, place_id):
                return resolved

        vals = {"home_city_place_id": "ChIJ_city",
                "home_city": "Mississauga", "home_province": "ON"}
        with patch(
                "odoo.addons.prema_owner_operator.controllers.main."
                "GooglePlacesService", FakePlaces):
            out = OwnerOperatorController()._resolve_home_city(
                vals, [], self.env)
        self.assertTrue(out["home_location_verified"])
        self.assertFalse(out["home_location_review"])
        self.assertEqual(out["home_city"], "Mississauga")
        self.assertEqual(out["home_province"], "Ontario")
        self.assertEqual(out["home_province_code"], "ON")
        self.assertEqual(out["home_latitude"], 43.589)
        self.assertEqual(out["home_longitude"], -79.644)

    def test_manual_location_fallback_creates_review(self):
        controller = OwnerOperatorController()
        out = controller._resolve_home_city(
            {"home_city": "Sudbury", "home_province": "ON"}, [], self.env)
        self.assertFalse(out.get("home_location_verified", False))
        self.assertTrue(out["home_location_review"])
        self.assertIn("manual", out["home_location_review_reason"].lower())

        class NoPlaces:
            def __init__(self, env):
                pass

            def resolve_place(self, place_id):
                return None

        with patch(
                "odoo.addons.prema_owner_operator.controllers.main."
                "GooglePlacesService", NoPlaces):
            out = controller._resolve_home_city(
                {"home_city_place_id": "missing", "home_city": "Sudbury",
                 "home_province": "ON"}, [], self.env)
        self.assertTrue(out["home_location_review"])
        self.assertFalse(out["home_location_verified"])

    def test_licence_and_plate_dropdowns_have_exactly_thirteen(self):
        expected = {code for code, _name in PROVINCE_CHOICES}
        self.assertEqual(len(expected), 13)
        for fname in ("licence_province", "plate_province",
                      "home_province_code"):
            field = self.Application._fields[fname]
            codes = {value for value, _label in field.selection}
            self.assertEqual(codes, expected, fname)

    def test_make_and_model_are_separate_and_persist(self):
        vals, errors = normalise_public(_raw(), strict=True)
        self.assertFalse(errors)
        app = self.Application._create_from_public(vals)
        self.assertEqual(app.truck_make, "Freightliner")
        self.assertEqual(app.truck_model, "M2 106")
        self.assertEqual(app.equipment_model, "Freightliner M2 106")
        self.assertEqual(app.truck_year, "2013")
        self.assertEqual(app.transmission, "automatic")

    def test_grouped_numbers_persist_as_integers(self):
        vals, errors = normalise_public(_raw(), strict=True)
        self.assertFalse(errors)
        app = self.Application._create_from_public(vals)
        self.assertEqual(app.odometer_km, 530000)
        self.assertEqual(app.payload_lbs, 12000)
        self.assertEqual(app.gvwr_lbs, 26000)
        self.assertIsInstance(int(app.odometer_km), int)
        self.assertIsInstance(int(app.payload_lbs), int)
        self.assertIsInstance(int(app.gvwr_lbs), int)

    def test_cargo_van_and_straight_require_payload_and_dimensions(self):
        for vehicle in ("cargo_van", "straight"):
            _vals, errors = normalise_public(_raw(
                VehicleType=vehicle, LegalPayloadLb="", PalletCapacity="",
                CargoLengthFt="", CargoWidthIn="", CargoHeightIn="",
                DoorWidthIn="", DoorHeightIn=""), strict=True)
            joined = " ".join(errors)
            for label in ("legal payload", "pallet capacity", "cargo length",
                          "cargo width", "cargo height", "rear door width",
                          "rear door height"):
                self.assertIn(label, joined, (vehicle, label))

    def test_tractor_hides_and_clears_trailer_and_reefer(self):
        vals, errors = normalise_public(_raw(
            VehicleType="tractor", TrailerType="reefer",
            TrailerYear="2020", TrailerUnit="TR-1",
            TrailerLengthFt="53", CargoBodyType="reefer",
            LegalPayloadLb="10000", PalletCapacity="12",
            CargoLengthFt="53", CargoWidthIn="100", CargoHeightIn="110",
            DoorWidthIn="96", DoorHeightIn="110", Liftgate="yes",
            LiftgateCapacityLb="3000",
            ReeferTemperatureRange="-20 to 4"), strict=True)
        self.assertFalse(errors)
        app = self.Application._create_from_public(vals)
        self.assertEqual(app.trailer_ownership, "power_only")
        self.assertEqual(app.reefer_applicable, "not_applicable")
        for field in ("trailer_type", "trailer_year", "trailer_unit",
                      "trailer_length_ft", "cargo_body_type", "payload_lbs",
                      "pallet_capacity", "box_length_ft", "cargo_width_in",
                      "cargo_height_in", "door_width_in",
                      "door_clearance_in", "liftgate",
                      "liftgate_capacity_lb", "reefer_temperature_range"):
            self.assertFalse(app[field], field)

    def test_semi_requires_trailer_details_and_stores_them(self):
        _vals, errors = normalise_public(_raw(
            VehicleType="semi", TrailerType="dry",
            TrailerYear="", TrailerUnit="", TrailerLengthFt=""),
            strict=True)
        joined = " ".join(errors)
        self.assertIn("trailer length", joined)
        self.assertIn("trailer model year", joined)
        vals, errors = normalise_public(_raw(
            VehicleType="semi", TrailerType="reefer",
            TrailerYear="2022", TrailerUnit="TR-9", TrailerLengthFt="53",
            ReeferTemperatureRange="-18 to 4 C"), strict=True)
        self.assertFalse(errors)
        app = self.Application._create_from_public(vals)
        self.assertEqual(app.trailer_ownership, "own")
        self.assertEqual(app.trailer_type, "reefer")
        self.assertEqual(app.trailer_year, "2022")
        self.assertEqual(app.trailer_unit, "TR-9")
        self.assertEqual(app.trailer_length_ft, 53)
        self.assertEqual(app.reefer_applicable, "yes")

    def test_non_reefer_configuration_stores_not_applicable(self):
        vals, errors = normalise_public(_raw(
            CargoBodyType="dry", ReeferTemperatureRange="-20 to 4"),
            strict=True)
        self.assertFalse(errors)
        app = self.Application._create_from_public(vals)
        self.assertEqual(app.reefer_applicable, "not_applicable")
        self.assertFalse(app.reefer_temperature_range)

    def test_operating_areas_and_work_preferences_are_multi_value(self):
        vals, errors = normalise_public(_raw(
            ServiceProvinceAB="Alberta", ServiceProvinceBC="British Columbia",
            WorkPreferenceLonghaul="1"), strict=True)
        self.assertFalse(errors)
        app = self.Application._create_from_public(vals)
        self.assertEqual(
            set(app.service_province_ids.mapped("code")),
            {"ON", "QC", "AB", "BC"})
        self.assertEqual(
            set(app.work_preference_ids.mapped("code")),
            {"local", "regional", "longhaul"})
        self.assertFalse(vals.get("preferred_service_area_codes"))


@tagged("post_install", "-at_install")
class TestPublicForm126(HttpCase):

    def setUp(self):
        super().setUp()
        job = self.env["hr.job"].browse(5)
        if job.exists():
            job.write({"is_published": True})
        self.env["ir.config_parameter"].sudo().set_param(
            "prema_owner_operator.job_id", 5)

    def test_form_has_seven_steps_and_current_fields(self):
        page = self.url_open("/owner-operator/apply")
        self.assertEqual(page.status_code, 200)
        text = page.text
        self.assertEqual(text.count('class="o_oo_step'), 7)
        self.assertEqual(text.count('id="oo_progress"'), 1)
        self.assertIn("Full legal name", text)
        self.assertIn(
            "including middle names or initials", text)
        self.assertIn('name="TruckMake"', text)
        self.assertIn('name="TruckModel"', text)
        self.assertIn('name="TruckPlate"', text)
        self.assertIn('name="TruckPlateProvince"', text)
        self.assertIn('name="GVWRLb"', text)
        self.assertIn('name="DriverLicenceFront"', text)
        self.assertIn('name="DriverLicenceBack"', text)
        self.assertEqual(text.count('name="ServiceProvince'), 13)
        self.assertEqual(text.count('name="WorkPreference'), 4)
        self.assertNotIn("PreferredArea", text)
        self.assertNotIn("LiftgatePlatformLengthIn", text)
        self.assertNotIn("LiftgatePlatformWidthIn", text)
        self.assertNotIn("DockHeightCapable", text)
        self.assertIn('data-group="1"', text)
        self.assertIn("<span>km</span>", text)
        self.assertIn("<span>lb</span>", text)
        self.assertIn("Verified city", text)

    def test_one_browser_submission_creates_one_linked_application(self):
        page = self.url_open("/owner-operator/apply")
        token = re.search(
            r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        payload = _raw()
        payload["csrf_token"] = token
        response = self.url_open("/owner-operator/apply", data=payload)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Thank you", response.text)
        apps = self.env["prema.owner.operator.application"].search(
            [("email", "=", payload["email_from"])])
        self.assertEqual(len(apps), 1)
        app = apps
        self.assertEqual(app.contact_name, "Form Driver")
        self.assertEqual(app.phone, "+14165553510")
        self.assertEqual(app.truck_make, "Freightliner")
        self.assertEqual(app.truck_model, "M2 106")
        self.assertEqual(app.license_plate, "ABC 123")
        self.assertEqual(app.plate_province, "ON")
        self.assertEqual(app.gvwr_lbs, 26000)
        self.assertEqual(app.odometer_km, 530000)
        self.assertEqual(app.home_province, "Ontario")
        self.assertEqual(app.home_province_code, "ON")
        self.assertTrue(app.home_location_review)
        self.assertEqual(
            set(app.service_province_ids.mapped("code")), {"ON", "QC"})
        self.assertEqual(
            set(app.work_preference_ids.mapped("code")),
            {"local", "regional"})
        self.assertEqual(app.load_notification_channel, "email")
        self.assertEqual(app.load_notification_email, app.email)
        self.assertEqual(app.reefer_applicable, "not_applicable")
        self.assertEqual(
            self.env["prema.owner.operator.application"].search_count(
                [("hr_applicant_id", "=", app.hr_applicant_id.id)]), 1)
        self.assertEqual(len(app.document_ids.filtered(
            lambda d: d.document_type == "licence_front")), 1)
        self.assertEqual(len(app.document_ids.filtered(
            lambda d: d.document_type == "licence_back")), 1)


@tagged("post_install", "-at_install")
class TestLicenceUploads126(HttpCase):
    """Both licence images are private and open only on the scoped route."""

    def setUp(self):
        super().setUp()
        job = self.env["hr.job"].browse(5)
        self.env["ir.config_parameter"].sudo().set_param(
            "prema_owner_operator.job_id", job.id or 5)
        candidate = self.env["hr.candidate"].create({
            "partner_name": "Licence Tester",
            "email_from": "licence-%s@test.invalid" % uuid.uuid4().hex[:8],
        })
        applicant = self.env["hr.applicant"].create({
            "candidate_id": candidate.id,
            "job_id": job.id,
            "partner_name": "Licence Tester",
            "email_from": candidate.email_from,
        })
        self.app = applicant.owner_operator_application_id
        self.reviewer = self.env["res.users"].create({
            "name": "Licence Reviewer",
            "login": "lic.reviewer.%s" % uuid.uuid4().hex[:8],
            "password": "Reviewer#123",
            "groups_id": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref(
                    "prema_owner_operator.group_owner_operator").id])],
        })
        self.outsider = self.env["res.users"].create({
            "name": "Licence Outsider",
            "login": "lic.outsider.%s" % uuid.uuid4().hex[:8],
            "password": "Outsider#123",
            "groups_id": [(6, 0, [self.env.ref("base.group_user").id])],
        })
        self.docs = {}
        for doc_type, name in (("licence_front", "front.pdf"),
                               ("licence_back", "back.pdf")):
            doc = self.app.document_ids.filtered(
                lambda d, t=doc_type: d.document_type == t)[:1]
            attachment = self.env["ir.attachment"].create({
                "name": name,
                "raw": b"%PDF-1.4 licence " + doc_type.encode(),
                "mimetype": "application/pdf",
                "res_model": "prema.owner.operator.document",
                "res_id": doc.id,
                "public": False,
            })
            doc.write({"attachment_id": attachment.id, "status": "received"})
            self.docs[doc_type] = doc

    def test_both_licence_images_are_private_and_open_for_reviewer(self):
        for doc_type, doc in self.docs.items():
            self.assertFalse(doc.attachment_id.public,
                             "%s must not be public" % doc_type)
            self.authenticate(self.reviewer.login, "Reviewer#123")
            ok = self.url_open("/owner_operator/document/%s" % doc.id)
            self.assertEqual(ok.status_code, 200, doc_type)
            self.assertEqual(ok.headers.get("Content-Type"),
                             "application/pdf")
            self.authenticate(self.outsider.login, "Outsider#123")
            denied = self.url_open("/owner_operator/document/%s" % doc.id)
            self.assertEqual(denied.status_code, 403, doc_type)

# -*- coding: utf-8 -*-
"""18.0.1.2.7 — approval → driver contact → Documents → Fleet hand-off."""
import base64
import os
import uuid

from lxml import etree

from odoo import fields
from odoo.exceptions import AccessError, MissingError
from odoo.tests import tagged

from odoo.addons.prema_owner_operator.application_mapping import (
    PROVINCE_CHOICES)
from odoo.addons.prema_owner_operator.tests.test_owner_operator import (
    OwnerOperatorCase)
from odoo.addons.prema_owner_operator.tests.test_owner_operator_workflow import (
    _pdf_bytes)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAwMCAO+ip1sAAAAASUVORK5CYII=")


@tagged("post_install", "-at_install")
class TestFleetHandoff(OwnerOperatorCase):

    def _signed_request(self, app):
        pdf = _pdf_bytes()
        if not pdf:
            self.skipTest("reportlab is unavailable to build a test PDF")
        attachment = self.env["ir.attachment"].create({
            "name": "agreement.pdf", "raw": pdf,
            "mimetype": "application/pdf",
        })
        template = self.env["sign.template"].create({
            "attachment_id": attachment.id,
        })
        role = self.env.ref("sign.sign_item_role_default")
        request = self.env["sign.request"].with_context(
            no_sign_mail=True).create({
                "template_id": template.id,
                "reference": app.name,
                "subject": "Owner-Operator Agreement — %s" % app.name,
                "request_item_ids": [(0, 0, {
                    "partner_id": app.partner_id.id,
                    "role_id": role.id,
                })],
            })
        request.write({"state": "signed"})
        request.request_item_ids.write({
            "state": "completed",
            "signing_date": fields.Date.today(),
        })
        request.write({"completed_document": base64.b64encode(pdf)})
        app.write({
            "sign_request_id": request.id,
            "sign_template_id": template.id,
            "contract_sent_on": fields.Datetime.now(),
        })
        return request

    def _eligible(self, sign=True, **overrides):
        vals = self._public_vals(
            email="fleet-%s@test.invalid" % uuid.uuid4().hex[:8], **overrides)
        app = self.Application._create_from_public(vals)
        self._complete(app, sign=True)
        if overrides:
            app.write({
                key: value for key, value in overrides.items()
                if key in app._fields})
        app.write({
            "licence_number": "D-1234-5678",
            "licence_expiry": fields.Date.add(
                fields.Date.context_today(app), years=2),
            "licence_verification_status": "verified",
            "licence_verified_on": fields.Datetime.now(),
            "licence_verified_by": self.env.user.id,
        })
        if sign:
            self._signed_request(app)
        return app

    def _docs(self, app, role):
        return self.env["documents.document"].search([
            ("oo_application_id", "=", app.id),
            ("oo_role", "=", role),
        ])

    def _portal(self):
        return self.env["res.users"].create({
            "name": "Portal User",
            "login": "portal-%s" % uuid.uuid4().hex[:8],
            "share": True,
            "groups_id": [(6, 0, [self.env.ref("base.group_portal").id])],
        })

    def _assert_denied(self, doc, user, label):
        try:
            doc.with_user(user).check_access("read")
        except (AccessError, MissingError):
            return
        self.fail("%s could read %s" % (label, doc.name))

    # 1 -----------------------------------------------------------------
    def test_first_approval_creates_three_links_and_repeat_is_idempotent(self):
        app = self._eligible()
        business = app.partner_id
        driver_count = self.env["res.partner"].search_count(
            [("ref", "=", app.name)])
        vehicle_count = self.env["fleet.vehicle"].search_count(
            [("application_id", "=", app.id)])
        doc_count = self.env["documents.document"].search_count(
            [("oo_application_id", "=", app.id)])

        vehicle = app.action_approve()
        driver = app.driver_partner_id
        self.assertTrue(vehicle)
        self.assertTrue(driver)
        self.assertNotEqual(driver, business)
        self.assertFalse(driver.is_company)
        self.assertEqual(driver.parent_id, business)
        self.assertEqual(vehicle.driver_id, driver)
        self.assertEqual(vehicle.x_current_driver_contact_id, driver)
        self.assertEqual(app.fleet_vehicle_id, vehicle)
        docs_after_first = self.env["documents.document"].search_count(
            [("oo_application_id", "=", app.id)])
        self.assertTrue(docs_after_first)

        repeat = app.action_approve()
        self.assertEqual(repeat, vehicle)
        self.assertEqual(self.env["res.partner"].search_count(
            [("ref", "=", app.name)]), driver_count + 1)
        self.assertEqual(self.env["fleet.vehicle"].search_count(
            [("application_id", "=", app.id)]), vehicle_count + 1)
        self.assertEqual(self.env["documents.document"].search_count(
            [("oo_application_id", "=", app.id)]), docs_after_first)

    # 2 -----------------------------------------------------------------
    def test_phone_and_mobile_mapping(self):
        app = self._eligible(
            phone="+14165550100", whatsapp_number="+16475551234",
            load_notification_channel="whatsapp",
            whatsapp_consent_granted=True,
            use_contact_phone_for_whatsapp=False)
        app.action_approve()
        self.assertEqual(app.driver_partner_id.phone, "+14165550100")
        self.assertEqual(app.driver_partner_id.mobile, "+16475551234")

        same = self._eligible(
            phone="+14165550199", whatsapp_number="+14165550199",
            load_notification_channel="whatsapp",
            whatsapp_consent_granted=True,
            use_contact_phone_for_whatsapp=True)
        same.action_approve()
        self.assertEqual(same.driver_partner_id.phone, "+14165550199")
        self.assertEqual(same.driver_partner_id.mobile, "+14165550199")

    # 3 -----------------------------------------------------------------
    def test_all_thirteen_provinces_resolve_to_canadian_states(self):
        app = self._eligible()
        for code, name in PROVINCE_CHOICES:
            app.write({"home_province_code": code, "home_province": name})
            state = app._canadian_state()
            self.assertTrue(state, code)
            self.assertEqual(state.code, code)
            self.assertEqual(state.country_id.code, "CA")

    # 4 -----------------------------------------------------------------
    def test_licence_values_only_after_verification_and_private_documents(self):
        app = self._eligible()
        app.write({
            "licence_number": False,
            "licence_verification_status": "not_verified",
            "licence_verified_on": False,
            "licence_verified_by": False,
        })
        app.action_approve()
        driver = app.driver_partner_id
        self.assertFalse(driver.oo_licence_number)
        self.assertFalse(driver.oo_licence_expiry)
        self.assertFalse(driver.oo_licence_verified)

        app.write({
            "licence_number": "D-9999-0001",
            "licence_expiry": fields.Date.add(
                fields.Date.context_today(app), years=3),
        })
        app.action_verify_licence()
        app.action_approve()
        driver.invalidate_recordset()
        self.assertEqual(driver.oo_licence_number, "D-9999-0001")
        self.assertTrue(driver.oo_licence_verified)
        self.assertEqual(driver.oo_licence_class, "AZ")

        driver_docs = self._docs(app, "driver")
        types = set(driver_docs.mapped("oo_source_document_id.document_type"))
        self.assertIn("licence_front", types)
        self.assertIn("licence_back", types)
        self.assertIn("signed_agreement", types)
        portal = self._portal()
        public = self.env.ref("base.public_user")
        for doc in driver_docs:
            self.assertFalse(doc.attachment_id.public)
            self._assert_denied(doc, portal, "portal")
            self._assert_denied(doc, public, "public")

    # 5 -----------------------------------------------------------------
    def test_coi_versions_and_all_documents_are_on_the_vehicle(self):
        app = self._eligible()
        COI = self.env["prema.owner.operator.coi.version"].sudo()
        first, _ = COI._create_version(
            app, b"%PDF-1.4 first coi", "coi.pdf", "application/pdf",
            "broker@insurer.test", fields.Datetime.now(),
            "<first-%s@test.invalid>" % uuid.uuid4().hex)
        second, _ = COI._create_version(
            app, b"%PDF-1.4 second coi", "coi2.pdf", "application/pdf",
            "broker@insurer.test", fields.Datetime.now(),
            "<second-%s@test.invalid>" % uuid.uuid4().hex)
        # Storing a new version moves the checklist row back to RECEIVED.
        # Re-verify the current certificate so the approval gate is met.
        app.document_ids.filtered(
            lambda d: d.document_type == "coi_insurer")[:1].write({
                "status": "verified",
                "attachment_id": second.attachment_id.id,
            })
        vehicle = app.action_approve()
        docs = self._docs(app, "vehicle")
        version_ids = set(docs.mapped("oo_coi_version_id").ids)
        self.assertIn(first.id, version_ids)
        self.assertIn(second.id, version_ids)
        for version in (first, second):
            doc = docs.filtered(lambda d: d.oo_coi_version_id == version)
            self.assertEqual(len(doc), 1)
            self.assertEqual(doc.oo_source_checksum,
                             version.attachment_id.checksum)
            self.assertFalse(doc.attachment_id.public)
        photo_types = set(docs.mapped("oo_source_document_id.document_type"))
        for doc_type in ("photo_front", "photo_rear", "photo_left",
                         "photo_right", "photo_cargo_interior",
                         "licence_front", "licence_back", "signed_agreement"):
            self.assertIn(doc_type, photo_types)
        self.assertEqual(vehicle.application_id, app)
        portal = self._portal()
        self._assert_denied(docs[:1], portal, "portal")

    # 6 -----------------------------------------------------------------
    def test_vehicle_category_per_type_without_duplicates(self):
        expected = {
            "cargo_van": "Cargo Van",
            "straight": "Straight Truck",
            "tractor": "Tractor Truck",
            "semi": "Semi-Truck",
        }
        for vehicle_type, category_name in expected.items():
            app = self._eligible(vehicle_type=vehicle_type)
            if vehicle_type == "tractor":
                app.write({
                    "trailer_ownership": "power_only",
                    "cargo_body_type": False, "payload_lbs": 0,
                    "pallet_capacity": 0, "box_length_ft": 0,
                    "cargo_width_in": 0, "cargo_height_in": 0,
                    "door_width_in": 0, "door_clearance_in": 0,
                    "liftgate": "no",
                })
            elif vehicle_type == "semi":
                app.write({
                    "trailer_type": "dry", "trailer_year": "2022",
                    "trailer_unit": "TR-%s" % app.id,
                    "trailer_length_ft": 53,
                })
            vehicle = app.action_approve()
            self.assertEqual(vehicle.category_id.name, category_name)
            app.action_approve()
            self.assertEqual(self.env["fleet.vehicle.model.category"].search_count(
                [("name", "=", category_name)]), 1, category_name)

    # 7 -----------------------------------------------------------------
    def test_verified_vehicle_field_mapping_and_tractor_clearing(self):
        app = self._eligible(vehicle_type="straight")
        app.write({
            "gvwr_lbs": 26000, "license_plate": "abc 123",
            "plate_province": "ON", "home_city": "Mississauga",
            "home_province": "Ontario", "home_province_code": "ON",
            "home_latitude": 43.5852774, "home_longitude": -79.6449916,
            "home_city_place_id": "ChIJ_test",
            "payload_lbs": 12000, "pallet_capacity": 13,
            "box_length_ft": 26, "cargo_width_in": 100,
            "cargo_height_in": 96, "door_clearance_in": 87,
            "liftgate": "no", "air_ride": "yes",
        })
        vehicle = app.action_approve()
        self.assertEqual(vehicle.license_plate, "ABC 123")
        self.assertEqual(vehicle.oo_plate_province, "ON")
        self.assertEqual(vehicle.home_city, "Mississauga")
        self.assertEqual(vehicle.oo_home_province_code, "ON")
        self.assertEqual(vehicle.home_latitude, 43.5852774)
        self.assertEqual(vehicle.home_longitude, -79.6449916)
        self.assertEqual(vehicle.oo_home_place_id, "ChIJ_test")
        self.assertEqual(vehicle.x_gvwr_lbs, 26000)
        self.assertEqual(vehicle.x_max_payload_lbs, 12000)
        self.assertEqual(vehicle.x_max_pallets, 13)
        self.assertEqual(vehicle.x_cargo_box_length_ft, 26)
        self.assertEqual(vehicle.oo_cargo_width_in, 100)
        self.assertEqual(vehicle.x_box_interior_height_ft, 8.0)
        self.assertEqual(vehicle.oo_cargo_height_in, 96)
        self.assertEqual(vehicle.x_door_clearance_in, 87)
        self.assertEqual(vehicle.model_year, "2013")

        tractor = self._eligible(vehicle_type="tractor")
        tractor.write({
            "trailer_ownership": "power_only",
            "cargo_body_type": False, "payload_lbs": 0,
            "pallet_capacity": 0, "box_length_ft": 0,
            "cargo_width_in": 0, "cargo_height_in": 0,
            "door_width_in": 0, "door_clearance_in": 0,
            "liftgate": "no", "reefer_temperature_range": False,
        })
        tv = tractor.action_approve()
        for field in ("x_max_payload_lbs", "x_max_pallets",
                      "x_cargo_box_length_ft", "x_box_interior_height_ft",
                      "x_door_clearance_in", "oo_cargo_body_type",
                      "oo_cargo_width_in", "oo_trailer_type",
                      "oo_trailer_year", "oo_trailer_unit",
                      "oo_trailer_length_ft"):
            self.assertFalse(tv[field], field)
        self.assertFalse(tv.x_reefer)
        self.assertFalse(tv.x_liftgate)

        semi = self._eligible(vehicle_type="semi")
        semi.write({
            "trailer_type": "reefer", "trailer_year": "2021",
            "trailer_unit": "TR-SEMI", "trailer_length_ft": 53,
            "reefer_temperature_range": "-20 to 4 C",
        })
        sv = semi.action_approve()
        self.assertEqual(sv.oo_trailer_type, "reefer")
        self.assertEqual(sv.oo_trailer_year, "2021")
        self.assertEqual(sv.oo_trailer_unit, "TR-SEMI")
        self.assertEqual(sv.oo_trailer_length_ft, 53)
        self.assertTrue(sv.x_reefer)

    # 8 -----------------------------------------------------------------
    def test_front_photo_becomes_profile_image_only_when_valid(self):
        app = self._eligible()
        doc = app.document_ids.filtered(
            lambda d: d.document_type == "photo_front")[:1]
        attachment = self.env["ir.attachment"].create({
            "name": "front.png", "raw": PNG, "mimetype": "image/png",
        })
        doc.write({"attachment_id": attachment.id, "status": "verified"})
        vehicle = app.action_approve()
        self.assertTrue(vehicle.image_1920)
        self.assertTrue(vehicle.image_128)
        image_before = vehicle.image_1920

        doc.write({"status": "pending", "attachment_id": False})
        not_image = self.env["ir.attachment"].create({
            "name": "front.pdf",
            "raw": b"%PDF-1.4 not an image",
            "mimetype": "application/pdf",
        })
        doc.write({"status": "verified", "attachment_id": not_image.id})
        app.action_approve()
        self.assertEqual(vehicle.image_1920, image_before)

        no_photo = self._eligible()
        no_photo.document_ids.filtered(
            lambda d: d.document_type == "photo_front")[:1].write({
                "status": "verified",
                "attachment_id": self.env["ir.attachment"].create({
                    "name": "front.pdf",
                    "raw": b"%PDF-1.4 not an image",
                    "mimetype": "application/pdf",
                }).id,
            })
        vehicle2 = no_photo.action_approve()
        self.assertFalse(vehicle2.image_1920)

    # 9 -----------------------------------------------------------------
    def test_generated_tags_are_accurate_stable_and_manual_tags_survive(self):
        app = self._eligible(
            home_city="Mississauga", home_province="ON",
            home_province_code="ON", vehicle_type="straight")
        app.write({
            "cargo_body_type": "dry",
            "cargo_door_style": "rollup",
            "trailer_door_style": "rollup", "box_length_ft": 26,
            "gvwr_lbs": 26000, "air_ride": "yes", "liftgate": "no",
        })
        vehicle = app.action_approve()
        names = set(vehicle.tag_ids.mapped("name"))
        for expected in ("Type: Straight Truck", "Body: Dry freight",
                         "Length: 26 ft", "Home: Mississauga",
                         "Province: ON", "GVWR: 26,000 lb",
                         "Air Ride: Yes"):
            self.assertIn(expected, names)
        manual = self.env["fleet.vehicle.tag"].create({"name": "Manual pick"})
        vehicle.write({"tag_ids": [(4, manual.id)]})

        app.write({"box_length_ft": 30})
        app.action_approve()
        names = set(vehicle.tag_ids.mapped("name"))
        self.assertIn("Manual pick", names)
        self.assertIn("Length: 30 ft", names)
        self.assertNotIn("Length: 26 ft", names)
        self.assertNotIn(manual, app.generated_tag_ids)
        self.assertEqual(vehicle.home_latitude, app.home_latitude)
        self.assertEqual(vehicle.home_longitude, app.home_longitude)

    # 10 ----------------------------------------------------------------
    def test_operator_view_hides_only_requested_tabs(self):
        arch = self.env["fleet.vehicle"].get_view(False, "form")["arch"]
        root = etree.fromstring(arch.encode())
        for page_name in ("truck_costing", "daily_odometer_log",
                          "geotab_eld", "page_tax_info", "owner_operator"):
            nodes = root.xpath("//page[@name='%s']" % page_name)
            self.assertTrue(nodes, page_name)
            self.assertEqual(nodes[0].get("invisible"), "operator_id",
                             page_name)
        self.assertTrue(root.xpath("//field[@name='pallet_layout_ids']"))
        self.assertEqual(
            root.xpath("//field[@name='default_pallet_layout']")[0].get(
                "invisible"), "operator_id")
        self.assertTrue(root.xpath("//page[@name='truck_config']"))
        company = self.env["fleet.vehicle"].create({
            "name": "Company Truck", "model_id": self._vehicle_model().id,
        })
        self.assertFalse(company.operator_id)
        company.write({"x_operational_logistics": True})
        self.assertTrue(company.x_operational_logistics)

    # 11 ----------------------------------------------------------------
    def test_upgrade_leaves_existing_records_untouched(self):
        app = self._eligible()
        vehicle = app.action_approve()
        company = self.env["fleet.vehicle"].create({
            "name": "Untouched Company Truck",
            "model_id": self._vehicle_model().id,
            "x_operational_logistics": True,
        })
        before = {
            "apps": self.env["prema.owner.operator.application"].search_count([]),
            "partners": self.env["res.partner"].search_count([]),
            "vehicles": self.env["fleet.vehicle"].search_count([]),
            "attachments": self.env["ir.attachment"].search_count([]),
            "coi_versions": self.env[
                "prema.owner.operator.coi.version"].search_count([]),
            "stages": self.env["hr.applicant"].search([]).mapped(
                "stage_id").ids,
            "company_flag": company.x_operational_logistics,
            "company_odometer": company.odometer,
            "app_stage": app.hr_applicant_id.stage_id.id,
            "app_docs": app.document_ids.ids,
        }
        path = os.path.join(
            os.path.dirname(__file__), "..", "migrations",
            "18.0.1.2.7", "post-migrate.py")
        namespace = {}
        with open(path) as handle:
            exec(handle.read(), namespace)
        namespace["migrate"](self.env.cr, "18.0.1.2.6")
        after = {
            "apps": self.env["prema.owner.operator.application"].search_count([]),
            "partners": self.env["res.partner"].search_count([]),
            "vehicles": self.env["fleet.vehicle"].search_count([]),
            "attachments": self.env["ir.attachment"].search_count([]),
            "coi_versions": self.env[
                "prema.owner.operator.coi.version"].search_count([]),
            "stages": self.env["hr.applicant"].search([]).mapped(
                "stage_id").ids,
            "company_flag": company.x_operational_logistics,
            "company_odometer": company.odometer,
            "app_stage": app.hr_applicant_id.stage_id.id,
            "app_docs": app.document_ids.ids,
        }
        self.assertEqual(before, after)
        self.assertTrue(vehicle.operator_id)

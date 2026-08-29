# -*- coding: utf-8 -*-
"""18-section work order §22: 5-persona security audit.

Personas: anonymous / driver / customer / dispatcher / admin.

Model-level tests (TransactionCase, always run):
  - driver record rules: own jobs / stops / evidence only
  - driver ACLs: read+write but never unlink
  - services/dispatch_auth.check_job_access rejects foreign jobs
  - customer ownership rules: own bookings only, READ-ONLY (perm_write=0)
  - anonymous: no ORM access at all
  - dispatcher: unrestricted read of every job

Route-level tests (HttpCase — skipped automatically when the test run
has no HTTP server, i.e. --http-port=0):
  - /dispatch/driver: anonymous → login redirect, customer → 403,
    driver → 200
  - /my/bookings: anonymous → login redirect, customer sees own list,
    another customer's booking id → 404
  - /dispatch/track/<tracking>: public read-only tracking page

Run: --test-tags /prema_logistics_booking/tests/test_phase20_security_personas
"""
import datetime
import unittest

import odoo
from odoo.exceptions import AccessError
from odoo.tests import HttpCase, TransactionCase

from .test_phase12_evidence_relationships import PHOTO_A


def _mk_driver_user(env, name, login):
    partner = env["res.partner"].create({"name": name})
    return env["res.users"].create({
        "name": login, "login": login,
        "partner_id": partner.id, "tz": "UTC",
        "groups_id": [(6, 0, [
            env.ref("base.group_user").id,
            env.ref("prema_dispatch.group_dispatch_driver").id,
        ])],
    }), partner


def _mk_customer_user(env, name, login):
    partner = env["res.partner"].create({"name": name})
    return env["res.users"].create({
        "name": login, "login": login,
        "partner_id": partner.id, "tz": "UTC",
        "groups_id": [(6, 0, [
            env.ref("base.group_portal").id,
            env.ref("prema_logistics_booking.group_logistics_customer").id,
            env.ref("prema_logistics_booking.group_booking_beta_tester").id,
        ])],
    }), partner


def _mk_booking(env, partner, name):
    # booking_number is readonly-after-create, so it must be set at create
    # time exactly like BookingOrchestrationService does (readonly fields
    # are assignable on create, never on write).
    booking = env["logistics.booking"].create({
        "partner_id": partner.id,
        "shipment_type": "ltl", "service_mode": "dedicated",
        "load_type": "ltl", "temperature_mode": "dry",
        "equipment_requirement": "dry",
        "pallets": 1, "physical_pallets": 1, "weight_lbs": 2400.0,
        "pickup_date": datetime.date(2026, 9, 8),
        "estimated_delivery_date": datetime.date(2026, 9, 8),
        "price_snapshot": [{"line": "P20 test"}],
        "booking_number": env["logistics.booking"]._generate_booking_number(),
    })
    env["logistics.booking.stop"].create([
        {"booking_id": booking.id, "sequence": 10, "stop_type": "pickup",
         "city": "P20 Pickup", "pallet_count": 1},
        {"booking_id": booking.id, "sequence": 20, "stop_type": "delivery",
         "city": "P20 Delivery", "pallet_count": 1},
    ])
    return booking


class TestSecurityModelLevel(TransactionCase):
    """Persona access control at the ORM layer."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        env.user.write({"tz": "UTC"})

        cls.driver_a, cls.driver_a_partner = _mk_driver_user(
            env, "P20 Driver A", "p20drivera@test.local")
        cls.driver_b, cls.driver_b_partner = _mk_driver_user(
            env, "P20 Driver B", "p20driverb@test.local")
        cls.cust_a, cls.cust_a_partner = _mk_customer_user(
            env, "P20 Customer A", "p20custa@test.local")
        cls.cust_b, cls.cust_b_partner = _mk_customer_user(
            env, "P20 Customer B", "p20custb@test.local")
        cls.dispatch_user = env["res.users"].create({
            "name": "P20 Dispatcher", "login": "p20disp@test.local",
            "tz": "UTC",
            "groups_id": [(6, 0, [
                env.ref("base.group_user").id,
                env.ref("prema_dispatch.group_dispatcher").id,
            ])],
        })

        cls.booking_a = _mk_booking(env, cls.cust_a_partner, "P20 A")
        cls.booking_b = _mk_booking(env, cls.cust_b_partner, "P20 B")

        cls.job_a = cls.booking_a._create_dispatch_job()
        cls.job_a.write({"driver_id": cls.driver_a_partner.id})
        cls.job_b = cls.booking_b._create_dispatch_job()
        cls.job_b.write({"driver_id": cls.driver_b_partner.id})

        # Evidence on job A (uploaded as driver A).
        cls.ev_a = cls.job_a.with_user(cls.driver_a).driver_add_evidence(
            cls.job_a.stop_ids[0].id, "pop", PHOTO_A, "p20a.jpg")

    def test_a_driver_sees_only_own_jobs(self):
        """Record rule: driver A's ORM search returns only their own job."""
        jobs = self.env["prema.dispatch.job"].with_user(self.driver_a).search(
            [("id", "in", [self.job_a.id, self.job_b.id])])
        self.assertEqual(jobs.ids, [self.job_a.id])

    def test_b_driver_sees_only_own_stops(self):
        stops = self.env["prema.dispatch.stop"].with_user(self.driver_a).search(
            [("id", "in", self.job_a.stop_ids.ids + self.job_b.stop_ids.ids)])
        self.assertEqual(sorted(stops.ids), sorted(self.job_a.stop_ids.ids))

    def test_c_driver_sees_only_own_evidence(self):
        evs = self.env["prema.dispatch.evidence"].with_user(self.driver_a).search(
            [("job_id", "in", [self.job_a.id, self.job_b.id])])
        self.assertEqual([e.job_id.id for e in evs], [self.job_a.id])

    def test_d_driver_cannot_write_foreign_job(self):
        """perm_write=0 on the driver's own-job rule: writing job B as
        driver A must raise AccessError."""
        with self.assertRaises(AccessError):
            self.job_b.with_user(self.driver_a).write({"note": "p20 hijack"})

    def test_e_driver_cannot_unlink_own_job(self):
        """ACL grants read+write only — unlink denied even for own job."""
        with self.assertRaises(AccessError):
            self.job_a.with_user(self.driver_a).unlink()

    def test_f_auth_service_rejects_foreign_job(self):
        """dispatch_auth.check_job_access is the primary RPC guard."""
        from odoo.addons.prema_dispatch.services import dispatch_auth
        # Odoo 18's Environment has no with_user — build the env as the
        # driver via env(user=...) (the same call BaseModel.with_user
        # wraps).
        driver_env = self.env(user=self.driver_a.id)
        with self.assertRaises(AccessError) as ctx:
            dispatch_auth.check_job_access(driver_env, self.job_b)
        self.assertIn("access", str(ctx.exception).lower())
        # …and accepts the driver's own job.
        self.assertTrue(dispatch_auth.check_job_access(driver_env, self.job_a))

    def test_g_customer_sees_only_own_bookings(self):
        bs = self.env["logistics.booking"].with_user(self.cust_a).search(
            [("id", "in", [self.booking_a.id, self.booking_b.id])])
        self.assertEqual(bs.ids, [self.booking_a.id])

    def test_h_customer_is_read_only_even_on_own_booking(self):
        """Customer ownership rule carries perm_write=0: no mutation."""
        with self.assertRaises(AccessError):
            self.booking_a.with_user(self.cust_a).write(
                {"customer_notes": "p20 hijack"})

    def test_i_anonymous_has_no_orm_access(self):
        pub = self.env.ref("base.public_user")
        with self.assertRaises(AccessError):
            self.env["prema.dispatch.job"].with_user(pub).search(
                [("id", "=", self.job_a.id)])
        with self.assertRaises(AccessError):
            self.env["logistics.booking"].with_user(pub).search(
                [("id", "=", self.booking_a.id)])

    def test_j_dispatcher_sees_every_job(self):
        jobs = self.env["prema.dispatch.job"].with_user(
            self.dispatch_user).search(
            [("id", "in", [self.job_a.id, self.job_b.id])])
        self.assertEqual(sorted(jobs.ids), sorted([self.job_a.id, self.job_b.id]))

    def test_k_driver_has_no_booking_orm_access(self):
        """The driver app never exposes rates/revenue: driver group has no
        ACL on logistics.booking at all."""
        with self.assertRaises(AccessError):
            self.booking_a.with_user(self.driver_a).read(["calculated_price"])


@unittest.skipIf(
    odoo.service.server is None or odoo.service.server.server is None
    or odoo.service.server.server.httpd is None,
    "HTTP server not running (tests launched with --http-port=0)")
class TestSecurityRoutes(HttpCase):
    """Persona access control over real HTTP routes."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        env.user.write({"tz": "UTC"})

        cls.driver, cls.driver_partner = _mk_driver_user(
            env, "P20R Driver", "p20rdriver@test.local")
        cls.driver.password = "Audit@2026#"
        cls.cust_a, cls.cust_a_partner = _mk_customer_user(
            env, "P20R Customer A", "p20rcusta@test.local")
        cls.cust_a.password = "Audit@2026#"
        cls.cust_b, cls.cust_b_partner = _mk_customer_user(
            env, "P20R Customer B", "p20rcustb@test.local")
        cls.cust_b.password = "Audit@2026#"
        cls.plain = env["res.users"].create({
            "name": "P20R Plain", "login": "p20rplain@test.local",
            "tz": "UTC",
            "groups_id": [(6, 0, [env.ref("base.group_user").id])],
        })
        cls.plain.password = "Audit@2026#"

        cls.booking_a = _mk_booking(env, cls.cust_a_partner, "P20R A")
        cls.booking_b = _mk_booking(env, cls.cust_b_partner, "P20R B")
        cls.job_a = cls.booking_a._create_dispatch_job()
        cls.job_a.write({"driver_id": cls.driver_partner.id})
        if not cls.job_a.tracking_number:
            cls.job_a.write({"tracking_number": "P20RTRACK%s" % cls.job_a.id})

    def test_a_anonymous_driver_app_redirects_to_login(self):
        # url_open follows redirects by default — disable to see the
        # 303 itself.
        resp = self.url_open("/dispatch/driver", allow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/web/login", resp.headers.get("Location", ""))

    def test_b_plain_user_driver_app_403(self):
        self.authenticate("p20rplain@test.local", "Audit@2026#")
        resp = self.url_open("/dispatch/driver")
        self.assertEqual(resp.status_code, 403)

    def test_c_driver_driver_app_200(self):
        self.authenticate("p20rdriver@test.local", "Audit@2026#")
        resp = self.url_open("/dispatch/driver")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"driver", resp.content[:2000].lower())

    def test_d_anonymous_portal_bookings_redirects(self):
        resp = self.url_open("/my/bookings", allow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/web/login", resp.headers.get("Location", ""))

    def test_e_customer_own_bookings_visible(self):
        self.authenticate("p20rcusta@test.local", "Audit@2026#")
        resp = self.url_open("/my/bookings")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(self.booking_a.booking_number.encode(),
                      resp.content)
        self.assertNotIn(self.booking_b.booking_number.encode(),
                         resp.content)

    def test_f_customer_other_booking_detail_404(self):
        """Customer A requesting customer B's booking id gets a 404 — the
        ownership search returns nothing and the controller raises
        NotFound, never a redirect loop or a data leak."""
        self.authenticate("p20rcusta@test.local", "Audit@2026#")
        resp = self.url_open("/my/bookings/%d" % self.booking_b.id)
        self.assertEqual(resp.status_code, 404)

    def test_g_anonymous_tracking_page_public(self):
        resp = self.url_open(
            "/dispatch/track/%s" % self.job_a.tracking_number)
        self.assertEqual(resp.status_code, 200)

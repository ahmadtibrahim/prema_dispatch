# -*- coding: utf-8 -*-
"""18-section work order §22: performance baseline (N+1 audit).

HttpCase smoke tests that hit the four read-heavy portal/driver routes
and record per-request latency (printed at class teardown).  Run the
suite with --log-sql and count SELECT statements between the HTTP
request log lines to get the query count per route; actual counts are
recorded in the §22/final report, and the latency figures are the
regression tripwire: a page that suddenly takes 10x longer points at a
new N+1 loop before it reaches production.

Routes audited:
  - /dispatch/driver        (driver home: jobs + stops + load plan)
  - /my/bookings            (customer booking list)
  - /my/bookings/<id>       (customer booking detail)
  - /dispatch/track/<tn>    (anonymous public tracking)
"""
import time
import unittest

import odoo
from odoo.tests import HttpCase

from .test_phase20_security_personas import (
    _mk_customer_user, _mk_driver_user, _mk_booking,
)


@unittest.skipIf(
    odoo.service.server is None or odoo.service.server.server is None
    or odoo.service.server.server.httpd is None,
    "HTTP server not running (tests launched with --http-port=0)")
class TestPerfBaseline(HttpCase):
    """Measure real-HTTP latency on the read-heavy routes."""

    stats = {}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        env.user.write({"tz": "UTC"})

        cls.driver, cls.driver_partner = _mk_driver_user(
            env, "P22 Driver", "p22driver@test.local")
        cls.driver.password = "Audit@2026#"
        cls.cust, cls.cust_partner = _mk_customer_user(
            env, "P22 Customer", "p22cust@test.local")
        cls.cust.password = "Audit@2026#"

        # Seed enough rows that an N+1 loop shows up as a query-count
        # jump between this run and a 1-row baseline (compare with
        # --log-sql; the per-route windows are logged in the report).
        cls.booking = _mk_booking(env, cls.cust_partner, "P22")
        cls.job = cls.booking._create_dispatch_job()
        cls.job.write({"driver_id": cls.driver_partner.id})
        if not cls.job.tracking_number:
            cls.job.write({"tracking_number": "P22TRACK%s" % cls.job.id})
        cls.bookings = cls.booking
        for i in range(24):
            bk = _mk_booking(env, cls.cust_partner, "P22 %d" % i)
            job = bk._create_dispatch_job()
            job.write({"driver_id": cls.driver_partner.id})
            cls.bookings |= bk

    @classmethod
    def tearDownClass(cls):
        print("\n[P22 PERF BASELINE] %s"
              % ", ".join("%s=%.0fms" % (k, v) for k, v in sorted(cls.stats.items())))
        super().tearDownClass()

    def _record(self, key, elapsed_ms):
        """Keep the max — the slowest run is what a user experiences."""
        self.stats[key] = max(self.stats.get(key, 0.0), elapsed_ms)

    def _timed_get(self, url, expect=200, **kw):
        start = time.monotonic()
        resp = self.url_open(url, **kw)
        self._record(url, (time.monotonic() - start) * 1000)
        self.assertEqual(resp.status_code, expect, url)
        return resp

    def test_a_driver_home(self):
        self.authenticate("p22driver@test.local", "Audit@2026#")
        resp = self._timed_get("/dispatch/driver")
        self.assertIn(b"driver", resp.content[:2000].lower())

    def test_b_customer_bookings_list(self):
        self.authenticate("p22cust@test.local", "Audit@2026#")
        resp = self._timed_get("/my/bookings")
        self.assertIn(self.booking.booking_number.encode(), resp.content)

    def test_c_customer_booking_detail(self):
        self.authenticate("p22cust@test.local", "Audit@2026#")
        resp = self._timed_get("/my/bookings/%d" % self.booking.id)
        self.assertIn(self.booking.booking_number.encode(), resp.content)

    def test_d_public_tracking(self):
        resp = self._timed_get(
            "/dispatch/track/%s" % self.job.tracking_number)
        self.assertIn(b"track", resp.content[:2000].lower())
        # Unknown tracking number → 404 (no enumeration), and this second
        # request closes the --log-sql window for the first one.
        self._timed_get("/dispatch/track/NO-SUCH-TRACK-42", expect=404)

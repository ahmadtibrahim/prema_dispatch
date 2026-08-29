# -*- coding: utf-8 -*-
"""Section 11 — Customer tracking isolation (18-section work order §11).

"Customer-facing screens must not expose another customer's temperature or
shipment details" and "UI hiding alone is not security": the isolation must
hold server-side, at the ORM/RPC layer (record rules + ACLs), not merely in
template conditionals.

Two portal customers (distinct commercial partners), each with own bookings,
sessions, session stops. Verified:

  1. RPC scope on logistics.booking / .stop / .line (record rules) — a
     portal user sees only their own rows; reading another customer's row
     raises AccessError.
  2. ACL barrier on logistics.booking.pallet / .leg / .price.adjustment —
     no portal ACL row at all → AccessError on search.
  3. logistics.pricing.session.stop ownership rule (18.0.13.46.0) — the
     route-builder payload is scoped to the owning session's commercial
     partner; read AND write of another customer's session stop are
     refused. (Without this rule the customer group's read+write ACL made
     every customer's session stops globally readable.)
  4. Direct-URL semantics replicated from the controllers: /my/bookings/<id>
     ownership domain (404 for another customer's id) and /track's
     booking_number + tracking_token pair (token of customer B never
     resolves booking A; wrong number + valid token never resolves; the
     token is REQUIRED — sequential booking numbers alone cannot
     enumerate).
  5. Temperature/shipment fields: a portal user cannot read another
     customer's booking fields (including temperature_display) through the
     ORM at all — the booking record itself is invisible.

Run: --test-tags /prema_logistics_booking
"""
import datetime
import secrets

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase

from odoo.addons.prema_logistics_booking.controllers.tracking_portal import (
    STATUS_LABELS)


class TestPhase11CustomerIsolation(TransactionCase):
    """Two portal customers — server-side isolation on every surface."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        env.user.write({"tz": "UTC"})
        cls.partner_a = env["res.partner"].create(
            {"name": "Isolation Customer A"})
        cls.partner_b = env["res.partner"].create(
            {"name": "Isolation Customer B"})
        customer_group = env.ref(
            "prema_logistics_booking.group_logistics_customer")
        portal_group = env.ref("base.group_portal")
        cls.user_a = env["res.users"].create({
            "name": "iso-a", "login": "iso-a@test.local",
            "partner_id": cls.partner_a.id, "tz": "UTC",
            "groups_id": [(6, 0, [portal_group.id, customer_group.id])],
        })
        cls.user_b = env["res.users"].create({
            "name": "iso-b", "login": "iso-b@test.local",
            "partner_id": cls.partner_b.id, "tz": "UTC",
            "groups_id": [(6, 0, [portal_group.id, customer_group.id])],
        })
        cls.booking_a = cls._booking(env, cls.partner_a, "Iso A", reefer=True)
        cls.booking_b = cls._booking(env, cls.partner_b, "Iso B", reefer=False)
        cls.session_a = env["logistics.pricing.session"].create({
            "partner_id": cls.partner_a.id,
            "pickup_date": datetime.date(2026, 9, 1),
            "shipment_type": "ltl",
            "pallets": 1, "weight_lbs": 2400.0,
            "expires_at": fields.Datetime.now() + datetime.timedelta(days=1),
        })
        cls.session_b = env["logistics.pricing.session"].create({
            "partner_id": cls.partner_b.id,
            "pickup_date": datetime.date(2026, 9, 2),
            "shipment_type": "ltl",
            "pallets": 1, "weight_lbs": 2400.0,
            "expires_at": fields.Datetime.now() + datetime.timedelta(days=1),
        })
        cls.stop_a1 = cls._session_stop(env, cls.session_a, "A Route Stop 1")
        cls.stop_a2 = cls._session_stop(env, cls.session_a, "A Route Stop 2")
        cls.stop_b1 = cls._session_stop(env, cls.session_b, "B Route Stop 1")
        cls.stop_b2 = cls._session_stop(env, cls.session_b, "B Route Stop 2")

    @classmethod
    def _location(cls, env, name):
        return env["prema.dispatch.location"].create({
            "name": name, "address": f"12 {name} St, Ontario",
        })

    _booking_seq = 0

    @classmethod
    def _booking(cls, env, partner, tag, reefer):
        cls._booking_seq += 1
        vals = {
            "partner_id": partner.id,
            "booking_number": f"ISO-{cls._booking_seq:04d}-{tag.replace(' ', '')}",
            # High-entropy token: the /track authority is the PAIR
            # (number + token); sequential numbers alone must never resolve.
            "tracking_token": secrets.token_urlsafe(32),
            "shipment_type": "ltl",
            "temperature_mode": "reefer" if reefer else "dry",
            "service_mode": "dedicated", "load_type": "ltl",
            "equipment_requirement": "reefer" if reefer else "dry",
            "pallets": 1, "physical_pallets": 1, "weight_lbs": 2400.0,
            "pickup_date": datetime.date(2026, 9, 1),
            "estimated_delivery_date": datetime.date(2026, 9, 1),
            "price_snapshot": [{
                "line": "iso test",
                "_pallet_allocs": [{"pallet": 1, "stops": [1], "shared": False}],
            }],
        }
        if reefer:
            vals["required_temperature_c"] = -18.0
        booking = env["logistics.booking"].create(vals)
        env["logistics.booking.line"].create({
            "booking_id": booking.id, "sequence": 10,
            "description": f"{tag} line", "pallets": 1, "weight_lbs": 2400.0,
        })
        env["logistics.booking.stop"].create([
            {"booking_id": booking.id, "sequence": 10, "stop_type": "pickup",
             "saved_location_id": cls._location(env, f"{tag} Pickup").id,
             "location_name": f"{tag} Pickup",
             "city": "Pickup City", "pallet_count": 1},
            {"booking_id": booking.id, "sequence": 20, "stop_type": "delivery",
             "saved_location_id": cls._location(env, f"{tag} Delivery").id,
             "location_name": f"{tag} Delivery",
             "city": "Delivery City", "pallet_count": 1},
        ])
        return booking

    @classmethod
    def _session_stop(cls, env, session, name):
        return env["logistics.pricing.session.stop"].create({
            "session_id": session.id,
            "sequence": 10,
            "stop_type": "delivery",
            "location_name": name,
        })

    # ── 1. RPC scope: booking / stop / line ──────────────────────────

    def test_rpc_booking_scope(self):
        Booking = self.env["logistics.booking"].with_user(self.user_a)
        rows = Booking.search([], order="id")
        self.assertEqual(rows.ids, [self.booking_a.id])
        rows = Booking.search([("id", "in", [self.booking_a.id,
                                             self.booking_b.id])])
        self.assertEqual(rows.ids, [self.booking_a.id])
        # Reading the other customer's record raises server-side.
        with self.assertRaises(AccessError):
            Booking.browse(self.booking_b.id).read(["name"])
        with self.assertRaises(AccessError):
            Booking.browse(self.booking_b.id).temperature_display
        # The other user is scoped the same way, in the opposite direction.
        BookingB = self.env["logistics.booking"].with_user(self.user_b)
        self.assertEqual(BookingB.search([]).ids, [self.booking_b.id])
        with self.assertRaises(AccessError):
            BookingB.browse(self.booking_a.id).read(["name"])

    def test_rpc_stop_and_line_scope(self):
        Stop = self.env["logistics.booking.stop"].with_user(self.user_a)
        rows = Stop.search([])
        self.assertEqual(set(rows.mapped("booking_id").ids), {self.booking_a.id})
        with self.assertRaises(AccessError):
            Stop.browse(self.booking_b.stop_ids[0].id).read(["city"])
        Line = self.env["logistics.booking.line"].with_user(self.user_a)
        # Search is rule-filtered (only own lines appear)…
        rows = Line.search([("booking_id", "=", self.booking_b.id)])
        self.assertFalse(rows)
        # …and reading another customer's line record directly raises.
        with self.assertRaises(AccessError):
            Line.browse(self.booking_b.line_ids[0].id).read(["description"])
        # Staff (any internal user) is unaffected by the customer rules.
        all_rows = self.env["logistics.booking"].search([])
        self.assertIn(self.booking_a.id, all_rows.ids)
        self.assertIn(self.booking_b.id, all_rows.ids)

    # ── 2. ACL barrier: pallet / leg / price adjustment ─────────────

    def test_rpc_acl_barrier_models(self):
        Pallet = self.env["logistics.booking.pallet"].with_user(self.user_a)
        with self.assertRaises(AccessError):
            Pallet.search([])
        Leg = self.env["logistics.booking.leg"].with_user(self.user_a)
        with self.assertRaises(AccessError):
            Leg.search([])
        Adj = self.env["logistics.booking.price.adjustment"].with_user(
            self.user_a)
        with self.assertRaises(AccessError):
            Adj.search([])
        # The booking pallet count itself is served from the booking's
        # OWN stored fields — never from a child model the customer
        # cannot read.
        self.assertEqual(self.booking_a.physical_pallets, 1)

    # ── 3. pricing session stop ownership rule ──────────────────────

    def test_rpc_session_stop_scope(self):
        SessionStop = self.env["logistics.pricing.session.stop"].with_user(
            self.user_a)
        rows = SessionStop.search([])
        self.assertEqual(set(rows.mapped("session_id").ids),
                         {self.session_a.id})
        # Customer B's route-builder payload (locations!) is invisible…
        with self.assertRaises(AccessError):
            SessionStop.browse(self.stop_b1.id).read(["location_name"])
        # …and unwritable.
        with self.assertRaises(AccessError):
            SessionStop.browse(self.stop_b1.id).write(
                {"location_name": "tampered"})
        # User B sees exactly their own, symmetric.
        SessionStopB = self.env["logistics.pricing.session.stop"].with_user(
            self.user_b)
        self.assertEqual(set(SessionStopB.search([]).mapped("session_id").ids),
                         {self.session_b.id})
        # Staff sees all (rule is scoped to the customer group only).
        all_stops = self.env["logistics.pricing.session.stop"].search([])
        self.assertIn(self.stop_a1.id, all_stops.ids)
        self.assertIn(self.stop_b1.id, all_stops.ids)

    # ── 4. Direct-URL semantics (controller queries replicated) ──────

    def test_my_bookings_detail_ownership_domain(self):
        # my_booking_detail() scopes by commercial_partner_id — an id
        # belonging to another customer simply isn't found.
        partner = self.user_a.partner_id.commercial_partner_id
        found = self.env["logistics.booking"].with_user(
            self.user_a).sudo().search([
                ("id", "=", self.booking_b.id),
                ("commercial_partner_id", "=", partner.id),
            ], limit=1)
        self.assertFalse(found, "customer A must never resolve booking B")
        own = self.env["logistics.booking"].with_user(
            self.user_a).sudo().search([
                ("id", "=", self.booking_a.id),
                ("commercial_partner_id", "=", partner.id),
            ], limit=1)
        self.assertEqual(own.id, self.booking_a.id)

    def test_tracking_token_pair_isolation(self):
        # The /track controller searches by booking_number AND the
        # high-entropy token (sudo — the token pair IS the authority).
        Booking = self.env["logistics.booking"].sudo()

        def resolve(number, token):
            return Booking.search([
                ("booking_number", "=", number),
                ("tracking_token", "=", token),
            ], limit=1)

        # Correct pair resolves.
        self.assertTrue(resolve(
            self.booking_a.booking_number, self.booking_a.tracking_token))
        # Customer B's token must never resolve customer A's booking.
        self.assertFalse(resolve(
            self.booking_a.booking_number, self.booking_b.tracking_token))
        # Customer A's token must never resolve customer B's booking.
        self.assertFalse(resolve(
            self.booking_b.booking_number, self.booking_a.tracking_token))
        # Sequential booking numbers alone cannot enumerate: any random
        # token fails; an unknown number with a valid token fails.
        self.assertFalse(resolve(
            self.booking_a.booking_number, secrets.token_urlsafe(16)))
        self.assertFalse(resolve(
            "PF-999999-000001", self.booking_a.tracking_token))

    # ── 5. Temperature/shipment data never crosses customers ─────────

    def test_temperature_not_leaked_across_customers(self):
        # Booking A is a -18°C reefer shipment; its temperature_display is
        # its OWN booking's field — served through the booking rule. The
        # other customer's booking (and its temperature) is unreachable.
        booking_a = self.env["logistics.booking"].with_user(
            self.user_a).search([("id", "=", self.booking_a.id)])
        self.assertTrue(booking_a)
        with self.assertRaises(AccessError):
            self.env["logistics.booking"].with_user(
                self.user_a).browse(self.booking_b.id).temperature_display
        # Tracking page status labels are per-booking; the payload built
        # for booking A names only A's own stops.
        display = self.booking_a._tracking_stops_display()
        self.assertTrue(display)
        self.assertTrue(all(
            s["name"] != "Iso B Pickup" for s in display))
        self.assertIn("Iso A Pickup", [s["name"] for s in display])
        # STATUS_LABELS is pure mapping — no record access involved.
        self.assertIn("in_transit", STATUS_LABELS)

    def test_portal_booking_list_scoped(self):
        # my_bookings() lists by commercial_partner_id with record rules
        # also active — the union never leaks the other customer.
        partner = self.user_a.partner_id.commercial_partner_id
        rows = self.env["logistics.booking"].sudo().search([
            ("commercial_partner_id", "=", partner.id),
        ])
        self.assertEqual(rows.ids, [self.booking_a.id])
        partner_b = self.user_b.partner_id.commercial_partner_id
        rows_b = self.env["logistics.booking"].sudo().search([
            ("commercial_partner_id", "=", partner_b.id),
        ])
        self.assertEqual(rows_b.ids, [self.booking_b.id])

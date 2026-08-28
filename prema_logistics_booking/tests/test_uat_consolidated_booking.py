"""UAT regression — consolidated booking, pricing & driver (A–F).

Runs against a Prod-db copy (same convention as test_phases_3_10.py):
FSA regions and corridor departures are live production configuration and
are read-only inputs. Every test rolls back — nothing commits.

Matrix (work order §8):
  A  Single-stop behavior — 1 pickup → 1 delivery session/job sanity
  B  Consolidation — two bookings share one pickup physical visit
  C  Multi-pickup — 2 pickups → 1 delivery in ONE route, precedence,
     no duplicate receiver, no duplicate pallet allocs
  D  Temperature — 0°C preserved (not falsy), dry shows no setpoint,
     conflicting setpoints in a shared visit stay per-shipment
  E  Driver App serialization — get_driver_stops_for_date never hits the
     'list' object has no attribute 'filtered' crash; stops/visits carry
     setpoint + evidence keys
  F  Security — customer B cannot read/resolve customer A's data; the
     driver can load their route but cannot create grouping records
"""

from odoo.tests.common import TransactionCase

from odoo.addons.prema_logistics_booking.controllers.booking_portal import (
    _resolve_loc, _stop_loc_refs, _portal_coord_pair,
)
from odoo.addons.prema_logistics_booking.services.temperature_compat import (
    parse_required_temperature_c,
)
from odoo.addons.prema_logistics_booking.services.booking_orchestration_service import (
    BookingOrchestrationService,
)


class TestUatConsolidatedBooking(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env

        # ── Test customer A + its facilities/access rows ────────────
        cls.partner_a = env["res.partner"].create({
            "name": "UAT Consolidated Customer A",
            "is_company": True,
        })
        cls.partner_b = env["res.partner"].create({
            "name": "UAT Consolidated Customer B",
            "is_company": True,
        })

        Facility = env["prema.dispatch.location"]

        def mk_loc(name, addr, city, pc, lat, lng):
            return Facility.create({
                "name": name,
                "business_name": name,
                "address": addr,
                "street": addr,
                "city": city,
                "province_code": "ON",
                "postal_code": pc,
                "pin_lat": lat,
                "pin_lng": lng,
            })

        cls.ud = mk_loc("UAT United Dairy", "145 Sun Pac Blvd",
                        "Brampton", "L6S 5Z6", 43.711, -79.790)
        cls.tf = mk_loc("UAT Terra Freska", "1 Royal Gate Blvd",
                        "Vaughan", "L4L 8Z7", 43.771, -79.551)
        cls.hp = mk_loc("UAT Healthy Planet", "290 N Front St",
                        "Belleville", "K8P 3C4", 44.163, -77.381)
        cls.nf = mk_loc("UAT NOFRILLS", "100 College St",
                        "Belleville", "K8P 3H3", 44.183, -77.404)

        Access = env["logistics.location.customer.access"]
        cls.a_ud = Access.create({
            "commercial_partner_id": cls.partner_a.id,
            "facility_id": cls.ud.id,
            "customer_alias": "UAT United Dairy",
        })
        cls.a_tf = Access.create({
            "commercial_partner_id": cls.partner_a.id,
            "facility_id": cls.tf.id,
            "customer_alias": "UAT Terra Freska",
        })
        cls.a_hp = Access.create({
            "commercial_partner_id": cls.partner_a.id,
            "facility_id": cls.hp.id,
            "customer_alias": "UAT Healthy Planet",
        })
        cls.a_nf = Access.create({
            "commercial_partner_id": cls.partner_a.id,
            "facility_id": cls.nf.id,
            "customer_alias": "UAT NOFRILLS",
        })
        cls.b_ud = Access.create({
            "commercial_partner_id": cls.partner_b.id,
            "facility_id": cls.ud.id,
            "customer_alias": "UAT United Dairy (B)",
        })

        # ── Users ───────────────────────────────────────────────────
        cls.portal_a = env["res.users"].create({
            "name": "UAT Portal A",
            "login": "uat.portal.a@premafirm.com",
            "partner_id": cls.partner_a.id,
            "groups_id": [(6, 0, [
                env.ref("base.group_portal").id,
                env.ref("prema_logistics_booking.group_logistics_customer").id,
            ])],
        })
        cls.portal_b = env["res.users"].create({
            "name": "UAT Portal B",
            "login": "uat.portal.b@premafirm.com",
            "partner_id": cls.partner_b.id,
            "groups_id": [(6, 0, [
                env.ref("base.group_portal").id,
                env.ref("prema_logistics_booking.group_logistics_customer").id,
            ])],
        })
        cls.driver_partner = env["res.partner"].create({
            "name": "UAT Driver",
            "is_company": False,
        })
        cls.driver = env["res.users"].create({
            "name": "UAT Driver",
            "login": "uat.driver@premafirm.com",
            "partner_id": cls.driver_partner.id,
            "groups_id": [(6, 0, [
                env.ref("base.group_user").id,
                env.ref("prema_dispatch.group_dispatch_driver").id,
            ])],
        })
        # Drivers are internal users + the Driver group — the same
        # composition action_create_driver_account grants ("Internal user
        # (required for app access)").  The Driver record rules still scope
        # them to their own jobs; the security assertions below cover the
        # actual boundary (no route-visit CREATE, customer isolation).
        # fleet_vehicle.model_id is NOT NULL at DB level — create a brand +
        # model first (same pattern as test_phases_3_10._make_vehicle).
        cls.vehicle_model = env["fleet.vehicle.model"].create({
            "name": "UAT Test Model",
            "brand_id": env["fleet.vehicle.model.brand"].create(
                {"name": "UAT Test Brand"}).id,
        })
        cls.vehicle = env["fleet.vehicle"].create({
            "name": "UAT-TRK-01",
            "license_plate": "UAT TRK 01",
            "model_id": cls.vehicle_model.id,
        })

        cls.env_a = env(user=cls.portal_a.id)
        cls.env_b = env(user=cls.portal_b.id)
        cls.env_d = env(user=cls.driver.id)

    # ── Payload builders (mirror the live portal movement_v1 payload) ─
    def _build_payload(self, env_u, partner, pickups, delivery, temp_c,
                       load_type="ltl"):
        """pickups/delivery: list of (access_row, name, pallets, weight_lbs).
        Returns (normalized, service)."""
        raw_route_stops = [
            {"stop_key": f"PU{i+1}", "stop_type": "pickup",
             "saved_location_id": row.id, "location_name": name,
             "pallets": pal, "weight_lbs": wt}
            for i, (row, name, pal, wt) in enumerate(pickups)
        ] + [
            {"stop_key": "DL1", "stop_type": "delivery",
             "saved_location_id": delivery[0].id,
             "location_name": delivery[1],
             "pallets": delivery[2], "weight_lbs": delivery[3]},
        ]
        pallet_movements = []
        for i, (row, name, pal, wt) in enumerate(pickups):
            pallet_movements.append({
                "key": f"P{i+1}", "pickup_stop_key": f"PU{i+1}",
                "delivery_stop_keys": ["DL1"], "weight_lbs": wt,
                "shared": False,
            })
        total_wt = sum(wt for *_, wt in pickups)
        route_stops, pickup_stops, delivery_stops = [], [], []
        for rs in raw_route_stops:
            loc = _resolve_loc(env_u, partner, int(rs["saved_location_id"]))
            self.assertTrue(loc, f"unresolvable saved location {rs}")
            loc_eff = _portal_coord_pair(loc)
            entry = {
                "stop_key": rs["stop_key"],
                "location_name": rs["location_name"],
                "postal_code": (loc.postal_code or rs.get("postal_code")) or "",
                "latitude": loc_eff[0] or 0.0, "longitude": loc_eff[1] or 0.0,
                "address": loc.street or "", "city": loc.city or "",
                "pallets": int(rs.get("pallets") or 0),
                "weight_lbs": float(rs.get("weight_lbs") or 0.0),
                **_stop_loc_refs(loc),
                "timing_type": "flexible", "service_time_minutes": 15,
                "timezone": loc.timezone or "America/Toronto",
            }
            route_stops.append({
                "stop_key": entry["stop_key"], "stop_type": rs["stop_type"],
                "saved_location_id": int(rs["saved_location_id"]),
                "latitude": entry["latitude"], "longitude": entry["longitude"],
                "postal_code": entry["postal_code"], "address": entry["address"],
                "city": entry["city"], **_stop_loc_refs(loc),
            })
            (pickup_stops if rs["stop_type"] == "pickup" else delivery_stops).append(entry)

        service = BookingOrchestrationService(env_u)
        normalized = service.normalize_request({
            "partner_id": partner.id,
            "pickup_stops": pickup_stops,
            "delivery_stops": delivery_stops,
            "load_type": load_type,
            "equipment_type": "reefer" if temp_c is not None else "dry",
            "required_temperature_c": parse_required_temperature_c(
                f"{temp_c:g}") if temp_c is not None else None,
            "pallets": sum(pal for *_, pal, _ in pickups),
            "physical_pallets": sum(pal for *_, pal, _ in pickups),
            "shared_pallet_mode": False,
            "pallet_allocations": [
                {"pallet_index": i + 1, "pickup_loc_id": row.id,
                 "delivery_loc_id": delivery[0].id}
                for i, (row, *_) in enumerate(pickups)
            ],
            "route_model_version": "movement_v1",
            "route_stops": route_stops,
            "pallet_movements": pallet_movements,
            "weight_lbs": total_wt,
            "liftgate_pickup": False, "liftgate_delivery": False,
            "appointment": False, "residential": False,
            "same_day_requested": False,
            "pricing_method": "corridor",
            "requested_pickup_date": "2026-08-28",
        }, source_channel="portal")
        return normalized, service

    def _quote_confirm_job(self, pickups, delivery, temp_c, load_type="ltl"):
        """Full portal flow: quote → confirm → dispatch job."""
        normalized, service = self._build_payload(
            self.env_a, self.partner_a, pickups, delivery, temp_c, load_type)
        quote = service.prepare_quote(normalized, requested_departure_id=None)
        self.assertTrue(quote and quote.get("quote_token"),
                        "Get Price failed on first click")
        # pickups/delivery rows are the ACCESS rows (logistics.location
        # .customer.access); the physical address lives on the facility they
        # point to — mirroring what the live portal form would send.
        pu_fac = pickups[0][0].facility_id
        de_fac = delivery[0].facility_id
        booking = self.env_a["logistics.booking"].confirm_from_session(
            quote["quote_token"],
            {"pickup_postal_code": pu_fac.postal_code,
             "delivery_postal_code": de_fac.postal_code,
             "pickup_company": pickups[0][1],
             "delivery_company": delivery[1],
             "pickup_address": pu_fac.street,
             "delivery_address": de_fac.street},
        )
        jobs = booking._create_dispatch_job()
        return booking, jobs[0]

    def _assign(self, job):
        """Dispatcher-equivalent assignment: same driver + vehicle + date."""
        vals = {
            "driver_id": self.driver_partner.id,
            "operation_date": "2026-08-28",
        }
        if not job.corridor_departure_id:
            # Portal bookings on a scheduled departure are departure-
            # controlled: the departure owns the truck and a manual
            # vehicle write on the job is rejected by the sync guard.
            vals["vehicle_id"] = self.vehicle.id
        job.write(vals)
        return job

    # ── A. Single-stop behavior ─────────────────────────────────────
    def test_a_single_stop_quote_confirm_job(self):
        booking, job = self._quote_confirm_job(
            [(self.a_ud, "UAT United Dairy", 1, 750)],
            (self.a_hp, "UAT Healthy Planet", 1, 750),
            temp_c=2.0)
        self.assertTrue(booking.booking_number)
        self.assertEqual(len(job.stop_ids.filtered(
            lambda s: s.stop_type == "pickup")), 1)
        self.assertEqual(len(job.stop_ids.filtered(
            lambda s: s.stop_type == "dropoff")), 1)
        self.assertFalse(booking.invoice_id,
                         "no invoice at confirm (deferred)")

    # ── B. Consolidation ────────────────────────────────────────────
    def test_b_two_bookings_share_one_pickup_visit(self):
        b1, j1 = self._quote_confirm_job(
            [(self.a_ud, "UAT United Dairy", 1, 750)],
            (self.a_hp, "UAT Healthy Planet", 1, 750), temp_c=2.0)
        b2, j2 = self._quote_confirm_job(
            [(self.a_ud, "UAT United Dairy", 1, 750)],
            (self.a_nf, "UAT NOFRILLS", 1, 750), temp_c=None)  # dry
        self._assign(j1)
        self._assign(j2)
        day = self.env_d["prema.dispatch.job"].get_driver_stops_for_date(
            "2026-08-28")
        ud_pickup = [v for v in day.get("physical_visits") or []
                     if v.get("company_name") == "UAT United Dairy"
                     and v.get("type") == "pickup"]
        self.assertEqual(len(ud_pickup), 1,
                         "United Dairy must appear ONCE as pickup")
        names = {s.get("job_name") for s in ud_pickup[0].get("shipments") or []}
        self.assertIn(j1.name, names)
        self.assertIn(j2.name, names)

    # ── C. Multi-pickup ─────────────────────────────────────────────
    def test_c_two_pickups_one_delivery(self):
        booking, job = self._quote_confirm_job(
            [(self.a_ud, "UAT United Dairy", 1, 750),
             (self.a_tf, "UAT Terra Freska", 1, 750)],
            (self.a_hp, "UAT Healthy Planet", 2, 1500),
            temp_c=2.0)
        self.assertEqual(booking.physical_pallets, 2)
        self.assertEqual(booking.weight_lbs, 1500)
        pu = job.stop_ids.filtered(lambda s: s.stop_type == "pickup")
        de = job.stop_ids.filtered(lambda s: s.stop_type == "dropoff")
        self.assertEqual(len(pu), 2, "exactly 2 pickup stops")
        self.assertEqual(len(de), 1, "exactly 1 delivery stop")
        self.assertEqual(len(set(pu.mapped("saved_location_id").ids)), 2,
                         "two distinct pickup locations")
        self.assertEqual(len(set(de.mapped("saved_location_id").ids)), 1,
                         "one receiver, no duplicate delivery stop")
        self.assertLess(min(pu.mapped("sequence")), min(de.mapped("sequence")),
                        "pickup-before-delivery precedence")

    # ── D. Temperature ──────────────────────────────────────────────
    def test_d_zero_degrees_is_preserved(self):
        booking, job = self._quote_confirm_job(
            [(self.a_ud, "UAT United Dairy", 1, 750)],
            (self.a_hp, "UAT Healthy Planet", 1, 750), temp_c=0.0)
        self.assertEqual(booking.required_temperature_c, 0.0,
                         "0°C must be a real setpoint, not falsy")
        self.assertEqual(job.required_temperature_c, 0.0)
        self.assertTrue(job.requires_reefer)
        self.assertEqual(job.temp_requirement, "0 °C")

    def test_d_dry_shows_no_setpoint(self):
        booking, job = self._quote_confirm_job(
            [(self.a_ud, "UAT United Dairy", 1, 750)],
            (self.a_hp, "UAT Healthy Planet", 1, 750), temp_c=None)
        self.assertFalse(job.requires_reefer)
        self.assertFalse(job.required_temperature_c)
        self._assign(job)
        day = self.env_d["prema.dispatch.job"].get_driver_stops_for_date(
            "2026-08-28")
        for v in day.get("physical_visits") or []:
            for s in v.get("shipments") or []:
                if s.get("job_name") == job.name:
                    self.assertIs(
                        s.get("required_temperature_c"), False,
                        "dry shipment must not carry a numeric setpoint")
                    self.assertEqual(s.get("temperature_requirement"), "")

    def test_d_conflicting_setpoints_stay_per_shipment(self):
        b1, j1 = self._quote_confirm_job(
            [(self.a_ud, "UAT United Dairy", 1, 750)],
            (self.a_hp, "UAT Healthy Planet", 1, 750), temp_c=2.0)
        b2, j2 = self._quote_confirm_job(
            [(self.a_ud, "UAT United Dairy", 1, 750)],
            (self.a_hp, "UAT Healthy Planet", 1, 750), temp_c=8.0)
        self._assign(j1)
        self._assign(j2)
        day = self.env_d["prema.dispatch.job"].get_driver_stops_for_date(
            "2026-08-28")
        by_job = {}
        for v in day.get("physical_visits") or []:
            for s in v.get("shipments") or []:
                by_job.setdefault(s.get("job_name"), s)
        self.assertEqual(by_job[j1.name].get("required_temperature_c"), 2.0)
        self.assertEqual(by_job[j2.name].get("required_temperature_c"), 8.0)
        # Stops keep their own setpoint too.
        flags = {s.get("required_temperature_c")
                 for s in day.get("stops") or [] if s.get("requires_reefer")}
        self.assertTrue({2.0, 8.0} <= flags,
                        "conflicting setpoints must coexist per shipment")

    # ── E. Driver App serialization ─────────────────────────────────
    def test_e_driver_app_route_loads_and_carries_setpoint(self):
        booking, job = self._quote_confirm_job(
            [(self.a_ud, "UAT United Dairy", 1, 750),
             (self.a_tf, "UAT Terra Freska", 1, 750)],
            (self.a_hp, "UAT Healthy Planet", 2, 1500),
            temp_c=2.0)
        self._assign(job)
        # The 'list' object has no attribute 'filtered' crash regression:
        # get_driver_stops_for_date must handle a plain list of stops.
        day = self.env_d["prema.dispatch.job"].get_driver_stops_for_date(
            "2026-08-28")
        visits = day.get("physical_visits") or []
        new_visits = [v for v in visits if any(
            s.get("job_name") == job.name for s in v.get("shipments") or [])]
        self.assertEqual(
            len([v for v in new_visits if v.get("type") == "pickup"]), 2)
        self.assertEqual(
            len([v for v in new_visits if v.get("type") == "delivery"]), 1)
        for v in new_visits:
            for s in v.get("shipments") or []:
                if s.get("job_name") == job.name:
                    self.assertEqual(s.get("required_temperature_c"), 2.0)
                    self.assertEqual(s.get("temperature_requirement"), "2 °C")

    # ── F. Security / isolation ─────────────────────────────────────
    def test_f_customer_isolation_server_side(self):
        booking, job = self._quote_confirm_job(
            [(self.a_ud, "UAT United Dairy", 1, 750)],
            (self.a_hp, "UAT Healthy Planet", 1, 750), temp_c=2.0)
        # B cannot search A's booking (record rule).
        self.assertEqual(
            self.env_b["logistics.booking"].search_count(
                [("id", "=", booking.id)]), 0)
        # B cannot read A's booking.
        with self.assertRaises(Exception):
            self.env_b["logistics.booking"].browse(
                booking.id).read(["booking_number"])
        # B cannot resolve A's saved location (ownership in _resolve_loc).
        self.assertFalse(_resolve_loc(self.env_b, self.partner_b, self.a_ud.id))
        # B cannot see A's access rows (portal record rule).
        self.assertEqual(
            self.env_b["logistics.location.customer.access"].search_count(
                [("id", "=", self.a_ud.id)]), 0)
        # Portal has no ACL on dispatch jobs at all.
        with self.assertRaises(Exception):
            self.env_b["prema.dispatch.job"].search_count([])
        # Positive control: A still sees its own data.
        self.assertEqual(
            self.env_a["logistics.booking"].search_count(
                [("commercial_partner_id", "=", self.partner_a.id)]), 1)
        self.assertTrue(
            _resolve_loc(self.env_a, self.partner_a, self.a_ud.id))

    def test_f_driver_cannot_create_grouping_records(self):
        """The driver may load the route (materialization elevated) but
        still lacks create on route visit grouping records."""
        booking, job = self._quote_confirm_job(
            [(self.a_ud, "UAT United Dairy", 1, 750)],
            (self.a_hp, "UAT Healthy Planet", 1, 750), temp_c=2.0)
        self._assign(job)
        day = self.env_d["prema.dispatch.job"].get_driver_stops_for_date(
            "2026-08-28")
        self.assertTrue(day.get("physical_visits"))
        with self.assertRaises(Exception):
            self.env_d["prema.dispatch.route.visit"].create({})
        with self.assertRaises(Exception):
            self.env_d["prema.dispatch.route.visit.stop"].create({})

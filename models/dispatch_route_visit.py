from collections import defaultdict

from odoo import api, fields, models

class PremaDispatchRouteVisit(models.Model):
    _name = "prema.dispatch.route.visit"
    _description = "Physical Route Visit"
    _order = "operating_date, sequence, id"
    load_plan_id = fields.Many2one("prema.dispatch.load.plan", ondelete="cascade", index=True)
    operating_date = fields.Date(index=True)
    vehicle_id = fields.Many2one("fleet.vehicle", index=True); driver_id = fields.Many2one("res.partner", index=True)
    sequence = fields.Integer(default=10); visit_type = fields.Selection([("pickup", "Pickup"), ("delivery", "Delivery"), ("mixed", "Mixed"), ("other", "Other")], default="delivery")
    mixed_action_order = fields.Selection([
        ("unload_then_load", "Unload First, Then Load"),
        ("load_then_unload", "Load First, Then Unload"),
    ], string="Mixed Action Order", default="unload_then_load",
        help="Default action order for mixed visits (logical pickup + "
             "delivery at the same physical facility): UNLOAD first, "
             "then LOAD — unless constraints require otherwise.")
    saved_location_id = fields.Many2one("prema.dispatch.location", index=True); address = fields.Char(); effective_lat = fields.Float(digits=(10,6)); effective_lng = fields.Float(digits=(10,6))
    planned_arrival = fields.Datetime(); service_window = fields.Char(); status = fields.Selection([("pending", "Pending"), ("arrived", "Arrived"), ("completed", "Completed"), ("cancelled", "Cancelled")], default="pending")
    active = fields.Boolean(default=True); stop_link_ids = fields.One2many("prema.dispatch.route.visit.stop", "route_visit_id")

    @api.model
    def _compatible_stops(self, stops):
        """Return whether logical stops may share one physical visit.

        The visit is only a physical execution grouping.  The underlying
        jobs/stops remain separate.  Exact appointment conflicts are kept
        separate; ordinary/flexible windows may share a visit.
        """
        if isinstance(stops, list):
            stops = self.env["prema.dispatch.stop"].browse([s.id for s in stops])
        stops = stops.filtered(lambda s: s.status not in
                              ("completed", "cancelled", "skipped"))
        if not stops:
            return False
        locations = stops.mapped("saved_location_id")
        if len(locations) != 1 or not locations:
            return False
        vehicles = stops.mapped("job_id.vehicle_id")
        if len(vehicles) > 1:
            return False
        dates = set()
        for stop in stops:
            job_date = getattr(stop.job_id, "operation_date", False)
            if job_date:
                dates.add(job_date)
            elif stop.scheduled_time:
                dates.add(stop.scheduled_time.date())
        if len(dates) > 1:
            return False

        exact = [s for s in stops if getattr(s, "exact_time", False)]
        exact_times = {s.exact_time for s in exact if s.exact_time}
        if len(exact_times) > 1:
            return False
        # A pair of explicitly bounded windows that do not overlap cannot be
        # one physical visit. Empty bounds mean flexible and are compatible.
        starts = [s.earliest_time for s in stops if s.earliest_time]
        ends = [s.latest_time for s in stops if s.latest_time]
        if starts and ends and max(starts) > min(ends):
            return False
        return True

    @api.model
    def ensure_for_stops(self, stops):
        """Materialize compatible logical stops as physical route visits.

        Grouping identity is operating date + assigned vehicle + canonical
        saved location.  This deliberately does not use address text and
        never combines jobs, items, evidence, or lifecycle records.
        """
        stops = stops.filtered(lambda s: s.saved_location_id and
                              s.status != "cancelled")
        if not stops:
            return self.browse()
        Link = self.env["prema.dispatch.route.visit.stop"]
        linked_by_stop = {}
        for link in Link.search([("stop_id", "in", stops.ids), ("active", "=", True)]):
            if link.route_visit_id.active and link.route_visit_id.status != "cancelled":
                linked_by_stop[link.stop_id.id] = link.route_visit_id

        grouped = defaultdict(list)
        # Completed/skipped logical history may remain in an existing visit,
        # but must never be used to create or rewrite a new operational visit.
        for stop in stops.filtered(lambda s: s.status not in
                                   ("completed", "skipped")):
            job = stop.job_id
            op_date = getattr(job, "operation_date", False) or (
                stop.scheduled_time.date() if stop.scheduled_time else False)
            vehicle_id = job.vehicle_id.id if job.vehicle_id else False
            grouped[(op_date, vehicle_id, stop.saved_location_id.id)].append(stop)

        visits = self.browse()
        for key, candidates in grouped.items():
            candidates = self.env["prema.dispatch.stop"].browse(
                [stop.id for stop in candidates])
            if len(candidates) < 2:
                continue
            # Do not rewrite an already completed/locked visit.  Existing
            # links are reused, and only still-unlinked compatible stops join.
            existing = None
            for stop in candidates:
                if linked_by_stop.get(stop.id):
                    existing = linked_by_stop[stop.id]
                    break
            if existing:
                if existing.status == "completed":
                    visits |= existing
                    continue
                joinable = candidates.filtered(lambda s: not linked_by_stop.get(s.id))
                if joinable and self._compatible_stops(existing.stop_link_ids.mapped("stop_id") | joinable):
                    for stop in joinable:
                        Link.create({"route_visit_id": existing.id, "stop_id": stop.id})
                visits |= existing
                continue
            if not self._compatible_stops(candidates):
                continue
            first = sorted(candidates, key=lambda s: (s.sequence or 0, s.id))[0]
            types = set(candidates.mapped("stop_type"))
            visit_type = "mixed" if "pickup" in types and ("dropoff" in types or "delivery" in types) \
                else "pickup" if types == {"pickup"} else "delivery"
            visit = self.create({
                "operating_date": key[0],
                "vehicle_id": key[1] or False,
                "driver_id": first.job_id.driver_id.id if first.job_id.driver_id else False,
                "sequence": min(candidates.mapped("sequence") or [10]),
                "visit_type": visit_type,
                "saved_location_id": key[2],
                "address": first.address or "",
                "effective_lat": first.pin_lat or first.latitude or 0.0,
                "effective_lng": first.pin_lng or first.longitude or 0.0,
                "planned_arrival": first.scheduled_time or False,
            })
            Link.create([{"route_visit_id": visit.id, "stop_id": stop.id} for stop in candidates])
            for stop in candidates:
                linked_by_stop[stop.id] = visit
            visits |= visit
        return visits

    @api.model
    def physical_visits_payload(self, stops):
        """Serialize grouped physical visits while retaining logical detail.

        Consumers use ``stops`` for the individual job actions and
        ``shipments`` for the compact physical-visit card.  All freight and
        evidence data comes from the original logical stop serializer.
        """
        stops = stops.filtered(lambda s: s.status != "cancelled")
        self.ensure_for_stops(stops)
        Link = self.env["prema.dispatch.route.visit.stop"]
        by_stop = {}
        for link in Link.search([("stop_id", "in", stops.ids), ("active", "=", True)]):
            if link.route_visit_id.active and link.route_visit_id.status != "cancelled":
                by_stop.setdefault(link.route_visit_id.id,
                                   self.env["prema.dispatch.stop"].browse())
                by_stop[link.route_visit_id.id] |= link.stop_id

        groups = []
        seen = set()
        for visit_id, logical in by_stop.items():
            logical = logical & stops
            if not logical:
                continue
            visit = self.browse(visit_id)
            groups.append((visit.sequence or 0, visit.id, logical))
            seen.update(logical.ids)
        for stop in stops:
            if stop.id not in seen:
                groups.append((stop.sequence or 0, False, stop))
        groups.sort(key=lambda row: (row[0], min(row[2].mapped("id"))))

        Job = self.env["prema.dispatch.job"]
        out = []
        for order, (sequence, visit_id, logical) in enumerate(groups, start=1):
            logical = logical.sorted(lambda s: (s.sequence or 0, s.id))
            serialized = [Job._driver_stop_dict(stop) for stop in logical]
            for stop, payload in zip(logical, serialized):
                payload.update({
                    "job_id": stop.job_id.id,
                    "job_name": stop.job_id.name,
                    "job_partner": stop.job_id.partner_id.name if stop.job_id.partner_id else "",
                    "_physical_visit_id": visit_id or False,
                })
            first = logical[0]
            location_name = first.saved_location_id.business_name or first.saved_location_id.name or first.address or ""
            location = first.saved_location_id
            address_head = (location.address or "").split(",", 1)[0].strip().lower()
            if location.chain_name and (location_name or "").strip().lower() in {
                address_head, (location.name or "").strip().lower(), "",
            }:
                location_name = "%s - %s" % (location.chain_name, location.city) \
                    if location.city else location.chain_name
            physical_type = "mixed" if {s.stop_type for s in logical} == {"pickup", "dropoff"} else (
                "pickup" if all(s.stop_type == "pickup" for s in logical) else "delivery")
            picked = sum(len(s._items_picked_here()) for s in logical if s.stop_type == "pickup")
            delivered = sum(len(s._items_delivered_here()) for s in logical if s.stop_type in ("dropoff", "return"))
            jobs = {}
            for stop, payload in zip(logical, serialized):
                entry = jobs.setdefault(stop.job_id.id, {
                    "job_id": stop.job_id.id, "job_name": stop.job_id.name,
                    "booking_number": stop.job_id.logistics_booking_id.booking_number
                        if getattr(stop.job_id, "logistics_booking_id", False) else "",
                    "partner": stop.job_id.partner_id.name if stop.job_id.partner_id else "",
                    "stops": [], "pallets": 0, "weight_lbs": 0.0,
                    "pop_required": False, "pod_required": False,
                    "pop_count": 0, "pod_count": 0,
                })
                entry["stops"].append(payload)
                entry["pop_required"] = entry["pop_required"] or bool(payload.get("pop_required"))
                entry["pod_required"] = entry["pod_required"] or bool(payload.get("pod_required"))
                entry["pop_count"] += len(payload.get("pop_attachments") or [])
                entry["pod_count"] += len(payload.get("pod_attachments") or [])
                if stop.stop_type == "pickup":
                    entry["pallets"] += len(stop._items_picked_here())
                    entry["weight_lbs"] += sum(i.weight_lbs for i in stop._items_picked_here())
                elif stop.stop_type in ("dropoff", "return"):
                    items = stop._items_delivered_here()
                    entry["pallets"] += len(items)
                    for item in items:
                        allocations = item.stop_allocation_ids.filtered(
                            lambda a, st=stop: a.stop_id == st and a.active)
                        allocation_weight = sum(allocations.mapped("weight_lbs"))
                        entry["weight_lbs"] += allocation_weight or item.weight_lbs
            out.append({
                "id": "visit-%s" % (visit_id or first.id),
                "route_visit_id": visit_id or False,
                "job_id": first.job_id.id,
                "job_name": first.job_id.name,
                "job_ids": logical.mapped("job_id").ids,
                "sequence": sequence,
                "type": physical_type,
                "type_label": "Mixed" if physical_type == "mixed" else physical_type.title(),
                "status": "arrived" if any(s.status == "arrived" for s in logical) else first.status,
                "saved_location_id": first.saved_location_id.id,
                "company_name": location_name,
                "address": first.address or first.saved_location_id.address or "",
                "lat": first.pin_lat or first.latitude or first.saved_location_id.pin_lat or 0.0,
                "lng": first.pin_lng or first.longitude or first.saved_location_id.pin_lng or 0.0,
                "pallets_in": picked, "pallets_out": delivered,
                "shipments": list(jobs.values()),
                "stops": serialized,
            })
        return out

    @api.model
    def arrive_physical_visit(self, visit_id, lat=0.0, lng=0.0):
        """Record one physical arrival without completing logical stops."""
        visit = self.browse(int(visit_id))
        if not visit.exists() or not visit.active:
            return {"success": False, "error": "Physical visit not found."}
        from odoo.addons.prema_dispatch.services.dispatch_auth import check_stop_access
        stops = visit.stop_link_ids.filtered("active").mapped("stop_id")
        if not stops or any(not check_stop_access(self.env, stop, raise_on_fail=False) for stop in stops):
            return {"success": False, "error": "Not authorized for this physical visit."}
        for stop in stops:
            if stop.status not in ("completed", "cancelled", "skipped", "arrived"):
                stop.action_mark_arrived()
            stop.write({"gps_stamp_lat": lat or 0.0, "gps_stamp_lng": lng or 0.0,
                        "gps_stamp_time": fields.Datetime.now()})
        visit.write({"status": "arrived", "planned_arrival": visit.planned_arrival or fields.Datetime.now()})
        return {"success": True, "stop_ids": stops.ids, "route_visit_id": visit.id}

class PremaDispatchRouteVisitStop(models.Model):
    _name = "prema.dispatch.route.visit.stop"
    _description = "Physical Route Visit Stop Link"
    route_visit_id = fields.Many2one("prema.dispatch.route.visit", required=True, ondelete="cascade")
    stop_id = fields.Many2one("prema.dispatch.stop", required=True, ondelete="cascade")
    job_id = fields.Many2one("prema.dispatch.job", related="stop_id.job_id", store=True, index=True)
    completion_state = fields.Selection([("pending", "Pending"), ("completed", "Completed"), ("exception", "Exception")], default="pending")
    pod_required = fields.Boolean(related="stop_id.pod_required", store=True); active = fields.Boolean(default=True)

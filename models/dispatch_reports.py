from datetime import datetime, time, timedelta

from markupsafe import escape
import pytz
from dateutil.relativedelta import relativedelta

from odoo import api, exceptions, fields, models


def _fmt_stop_local(dt, tz_name):
    """Naive UTC -> 12-hour AM/PM in the stop's own local timezone."""
    if not dt:
        return "—"
    tz = pytz.timezone(tz_name) if tz_name else pytz.timezone("America/Toronto")
    local = pytz.utc.localize(dt).astimezone(tz)
    return local.strftime("%b %d, %I:%M %p").replace(" 0", " ")


def _job_hours(jobs):
    """Shared drive/detention-hours computation for the stop report and
    driver worksheets — kept as one small helper instead of two copies."""
    drive_min = 0
    detention_min = 0
    stop_count = 0
    for job in jobs:
        for s in job.stop_ids.filtered(lambda s: s.status != "cancelled"):
            stop_count += 1
            drive_min += s.drive_time_from_prev_minutes or 0
            if s.actual_arrival_time and s.actual_departure_time:
                actual_min = (s.actual_departure_time - s.actual_arrival_time).total_seconds() / 60
                detention_min += max(0, actual_min - (s.service_time_minutes or 15))
    return drive_min, detention_min, stop_count


def _stop_hours(stops):
    """Worksheet generation is day/stop based, not whole-job based."""
    drive_min = 0
    detention_min = 0
    stop_count = 0
    for stop in stops.filtered(lambda s: s.status != "cancelled"):
        stop_count += 1
        drive_min += stop.drive_time_from_prev_minutes or 0
        if stop.actual_arrival_time and stop.actual_departure_time:
            actual_min = (stop.actual_departure_time - stop.actual_arrival_time).total_seconds() / 60
            detention_min += max(0, actual_min - (stop.service_time_minutes or 15))
    return drive_min, detention_min, stop_count


def _fmt_hm(minutes):
    minutes = int(minutes or 0)
    return f"{minutes // 60}h {minutes % 60}m"


def _job_domain(driver_id=None, vehicle_id=None, date_from=None, date_to=None):
    """Shared job filter — same driver/truck/date-range shape used by the
    stop report wizard, reused by the Phase O reporting suite below."""
    domain = []
    if driver_id:
        domain.append(("driver_id", "=", driver_id.id))
    if vehicle_id:
        domain.append(("vehicle_id", "=", vehicle_id.id))
    if date_from:
        domain.append(("scheduled_pickup", ">=", date_from))
    if date_to:
        domain.append(("scheduled_pickup", "<=", date_to))
    return domain


def _reopen_wizard(wizard):
    return {
        "type": "ir.actions.act_window",
        "res_model": wizard._name,
        "res_id": wizard.id,
        "view_mode": "form",
        "target": "new",
    }


def _stop_on_time_info(stop, exact_tolerance_minutes=30):
    """Judge whether a completed stop arrived on time against whichever
    time window it had configured (exact appointment / deadline / window /
    plain scheduled_time — in that priority order).

    Returns (judged, on_time, late_minutes):
      - judged=False means there's nothing to compare the arrival against
        (no arrival recorded, or the stop was left "flexible" with no
        window at all) — such stops are excluded from on-time % rather
        than silently counted as on-time or late.
    """
    if not stop.actual_arrival_time:
        return False, False, 0.0
    arrival = stop.actual_arrival_time
    target = None
    tolerance = 0
    if stop.time_window_type == "exact" and stop.exact_time:
        target, tolerance = stop.exact_time, exact_tolerance_minutes
    elif stop.time_window_type == "deadline" and stop.deadline_time:
        target, tolerance = stop.deadline_time, 0
    elif stop.time_window_type == "window" and stop.latest_time:
        target, tolerance = stop.latest_time, 0
    elif stop.scheduled_time:
        target, tolerance = stop.scheduled_time, exact_tolerance_minutes
    if not target:
        return False, False, 0.0
    late_minutes = max(0.0, (arrival - target).total_seconds() / 60.0 - tolerance)
    return True, late_minutes <= 0, late_minutes


class DispatchStopReportWizard(models.TransientModel):
    _name = "prema.dispatch.stop.report.wizard"
    _description = "Stops / Driver Report Generator"

    driver_id = fields.Many2one("res.partner", string="Driver", domain="[('x_is_driver', '=', True)]")
    vehicle_id = fields.Many2one("fleet.vehicle", string="Truck")
    invoice_ref = fields.Char(string="Invoice Reference")
    disp_number = fields.Char(string="DISP Order #")
    date_from = fields.Date(default=lambda self: fields.Date.today())
    date_to = fields.Date(default=lambda self: fields.Date.today())
    summary_html = fields.Html(readonly=True)

    def action_generate(self):
        self.ensure_one()
        domain = []
        if self.driver_id:
            domain.append(("driver_id", "=", self.driver_id.id))
        if self.vehicle_id:
            domain.append(("vehicle_id", "=", self.vehicle_id.id))
        if self.disp_number:
            domain.append(("name", "ilike", self.disp_number))
        if self.invoice_ref:
            domain.append(("invoice_id.name", "ilike", self.invoice_ref))
        if self.date_from:
            domain.append(("scheduled_pickup", ">=", self.date_from))
        if self.date_to:
            domain.append(("scheduled_pickup", "<=", self.date_to))
        jobs = self.env["prema.dispatch.job"].search(domain, limit=200)

        drive_min, detention_min, stop_count = _job_hours(jobs)
        parts = [
            f"<p><b>{len(jobs)} job(s), {stop_count} stop(s)</b><br/>"
            f"Total drive time: {_fmt_hm(drive_min)}<br/>"
            f"Total detention time: {_fmt_hm(detention_min)}</p><hr/>"
        ]
        for job in jobs:
            parts.append(
                f"<h5>{job.name} — {job.driver_id.name or 'No driver'} / {job.vehicle_id.name or 'No truck'}</h5>"
                "<table class='table table-sm'><tr><th>Stop</th><th>Type</th>"
                "<th>Arrived</th><th>Departed</th><th>Status</th></tr>"
            )
            for s in job.stop_ids.sorted("sequence"):
                arr = _fmt_stop_local(s.actual_arrival_time, s.tz_name)
                dep = _fmt_stop_local(s.actual_departure_time, s.tz_name)
                parts.append(f"<tr><td>{s.name}</td><td>{s.stop_type}</td><td>{arr}</td><td>{dep}</td><td>{s.status}</td></tr>")
            parts.append("</table>")
        self.summary_html = "".join(parts)
        return _reopen_wizard(self)


class DispatchCustodyReportWizard(models.TransientModel):
    _name = "prema.dispatch.custody.report.wizard"
    _description = "Custody / Handoff Report — who picked up, transferred, and delivered each job"

    driver_id = fields.Many2one("res.partner", string="Driver", domain="[('x_is_driver', '=', True)]")
    vehicle_id = fields.Many2one("fleet.vehicle", string="Truck")
    disp_number = fields.Char(string="DISP Order #")
    date_from = fields.Date(default=lambda self: fields.Date.today())
    date_to = fields.Date(default=lambda self: fields.Date.today())
    summary_html = fields.Html(readonly=True)

    def action_generate(self):
        self.ensure_one()
        domain = _job_domain(self.driver_id, self.vehicle_id, self.date_from, self.date_to)
        if self.disp_number:
            domain.append(("name", "ilike", self.disp_number))
        jobs = self.env["prema.dispatch.job"].search(domain, limit=200)

        parts = [f"<p><b>{len(jobs)} job(s)</b> — pickup / handoff / delivery custody, one row per event.</p><hr/>"]
        for job in jobs:
            segs = job._job_segments()
            parts.append(
                f"<h5>{escape(job.name)} — {escape(job.partner_id.name or '')}</h5>"
                "<table class='table table-sm'><tr><th>Event</th><th>Location</th>"
                "<th>Truck / Driver</th><th>Time</th></tr>"
            )
            for i, seg in enumerate(segs):
                for st, role in seg["stops"]:
                    if role == "receiving":
                        continue  # already shown as the matching "giving" row's handoff-to side
                    driver = st.completed_driver_id or seg["driver"]
                    vehicle = st.completed_vehicle_id or seg["vehicle"]
                    who = f"{escape(vehicle.display_name) if vehicle else '—'} / {escape(driver.name) if driver else '—'}"
                    time_label = _fmt_stop_local(
                        st.actual_departure_time or st.actual_arrival_time, st.tz_name
                    )
                    if st.stop_type == "pickup":
                        event, detail = "Picked Up", who
                    elif st.stop_type in ("dropoff", "return"):
                        event, detail = "Delivered", who
                    elif role == "giving":
                        next_seg = segs[i + 1] if i + 1 < len(segs) else None
                        to_driver = next_seg["driver"] if next_seg else None
                        to_vehicle = next_seg["vehicle"] if next_seg else None
                        to_who = (
                            f"{escape(to_vehicle.display_name) if to_vehicle else '—'} / "
                            f"{escape(to_driver.name) if to_driver else '—'}"
                        )
                        event, detail = "Handoff", f"{who} → {to_who}"
                    else:
                        event, detail = job._stop_type_label(st.stop_type), who
                    parts.append(
                        f"<tr><td>{escape(event)}</td><td>{escape(st.address or '')}</td>"
                        f"<td>{detail}</td><td>{time_label}</td></tr>"
                    )
            parts.append("</table>")
        self.summary_html = "".join(parts)
        return _reopen_wizard(self)


class DispatchDriverWorksheet(models.Model):
    _name = "prema.dispatch.driver.worksheet"
    _description = "Driver Daily Worksheet"
    _order = "date desc, id desc"

    name = fields.Char(compute="_compute_name", store=True)
    driver_id = fields.Many2one("res.partner", string="Driver", required=True)
    vehicle_id = fields.Many2one("fleet.vehicle", string="Truck")
    date = fields.Date(required=True, default=fields.Date.today)
    job_ids = fields.Many2many("prema.dispatch.job", string="Jobs")
    stop_ids = fields.Many2many("prema.dispatch.stop", string="Stops", readonly=True)
    stop_count = fields.Integer(readonly=True)
    total_drive_minutes = fields.Integer(readonly=True)
    total_detention_minutes = fields.Integer(readonly=True)
    worksheet_html = fields.Html(readonly=True)
    is_active = fields.Boolean(
        default=True, readonly=True,
        help="Stays True — and stays on the live Driver Worksheets list — "
             "until every job on the sheet is completed.",
    )
    generated_at = fields.Datetime(default=fields.Datetime.now, readonly=True)

    @api.depends("driver_id", "date")
    def _compute_name(self):
        for w in self:
            w.name = f"{w.driver_id.name or '?'} — {w.date or ''}"

    @api.model
    def _worksheet_jobs_and_stops(self, vehicle_id, target_date):
        Job = self.env["prema.dispatch.job"]
        user_tz = pytz.timezone(self.env.user.tz or "America/Toronto")
        utc_start = user_tz.localize(datetime.combine(target_date, time.min)).astimezone(
            pytz.utc
        ).replace(tzinfo=None)
        utc_end = user_tz.localize(datetime.combine(target_date, time.max)).astimezone(
            pytz.utc
        ).replace(tzinfo=None)

        jobs = Job.search([
            ("vehicle_id", "=", vehicle_id),
            ("stage_id.is_cancelled", "=", False),
            "|",
            ("scheduled_pickup", "=", False),
            "&",
            ("scheduled_pickup", ">=", utc_start - timedelta(days=2)),
            ("scheduled_pickup", "<=", utc_end + timedelta(days=2)),
        ], limit=500)

        day_jobs = Job.browse()
        day_stops = self.env["prema.dispatch.stop"]
        for job in jobs:
            stops = job.stop_ids.filtered(
                lambda s: s.status != "cancelled" and Job._stop_local_date(s, job, user_tz) == target_date
            )
            if stops:
                day_jobs |= job
                day_stops |= stops
        return day_jobs, day_stops

    @api.model
    def _worksheet_stop_payloads(self, stops):
        Job = self.env["prema.dispatch.job"]
        payloads = []
        for stop in stops:
            payload = Job._driver_stop_dict(stop)
            payload["job_id"] = stop.job_id.id
            payload["job_name"] = stop.job_id.name
            payloads.append(payload)
        payloads.sort(key=Job._serialized_stop_sort_key)
        Job._apply_truck_onboard_counts(payloads)
        return payloads

    @api.model
    def _worksheet_time_label(self, iso_value, tz_name):
        if not iso_value:
            return "—"
        dt = datetime.fromisoformat(str(iso_value).rstrip("Z"))
        return _fmt_stop_local(dt, tz_name)

    @api.model
    def _worksheet_html(self, stop_payloads):
        parts = [
            "<table class='table table-sm table-striped'>",
            "<thead><tr>",
            "<th>#</th><th>Job</th><th>Company</th><th>Action</th><th>Scheduled</th>",
            "<th>Address</th><th>In</th><th>Out</th><th>Onboard</th><th>Status</th>",
            "</tr></thead><tbody>",
        ]
        for idx, stop in enumerate(stop_payloads, start=1):
            scheduled = (
                stop.get("scheduled_time")
                or stop.get("estimated_arrival")
                or stop.get("actual_arrival_time")
            )
            parts.append(
                "<tr>"
                f"<td>{idx}</td>"
                f"<td>{escape(stop.get('job_name') or '')}</td>"
                f"<td>{escape(stop.get('company_name') or '')}</td>"
                f"<td>{escape(stop.get('type_label') or '')}</td>"
                f"<td>{escape(self._worksheet_time_label(scheduled, stop.get('tz_name')))}</td>"
                f"<td>{escape(stop.get('address') or '')}</td>"
                f"<td>{int(stop.get('pallets_in') or 0)}</td>"
                f"<td>{int(stop.get('pallets_out') or 0)}</td>"
                f"<td>{int(stop.get('onboard_after') or 0)}</td>"
                f"<td>{escape((stop.get('status') or '').replace('_', ' ').title())}</td>"
                "</tr>"
            )
        parts.append("</tbody></table>")
        return "".join(parts)

    @api.model
    def generate_for_truck(self, vehicle_id, date_str=None):
        """Called from the Dispatch Planner's "Sheet" button next to Optimize.
        One worksheet per truck+date — re-generating refreshes the numbers
        and job list instead of creating duplicates."""
        target_date = fields.Date.from_string(date_str) if date_str else fields.Date.context_today(self)
        vehicle = self.env["fleet.vehicle"].browse(vehicle_id)
        if not vehicle.exists():
            raise exceptions.UserError("Truck not found.")

        jobs, stops = self._worksheet_jobs_and_stops(vehicle_id, target_date)
        if not stops:
            raise exceptions.UserError(
                f"No planned stops found for {vehicle.name or 'this truck'} on {target_date}."
            )

        driver = jobs.mapped("driver_id")[:1] or vehicle.driver_id or vehicle.x_current_driver_contact_id
        if not driver:
            raise exceptions.UserError(
                "Assign a driver to this truck or its jobs before generating a worksheet."
            )

        stop_payloads = self._worksheet_stop_payloads(stops)
        drive_min, detention_min, stop_count = _stop_hours(stops)
        existing = self.search([
            ("vehicle_id", "=", vehicle_id), ("date", "=", target_date),
        ], limit=1)
        vals = {
            "vehicle_id": vehicle_id,
            "driver_id": driver.id,
            "date": target_date,
            "job_ids": [(6, 0, jobs.ids)],
            "stop_ids": [(6, 0, stops.ids)],
            "stop_count": stop_count,
            "total_drive_minutes": drive_min,
            "total_detention_minutes": detention_min,
            "worksheet_html": self._worksheet_html(stop_payloads),
            "is_active": True,
            "generated_at": fields.Datetime.now(),
        }
        if existing:
            existing.write(vals)
            return {
                "id": existing.id,
                "created": False,
                "message": f"Driver worksheet refreshed for {vehicle.name} on {target_date}.",
            }
        worksheet = self.create(vals)
        return {
            "id": worksheet.id,
            "created": True,
            "message": f"Driver worksheet generated for {vehicle.name} on {target_date}.",
        }

    def action_refresh(self):
        for w in self:
            self.generate_for_truck(w.vehicle_id.id, fields.Date.to_string(w.date))

    @api.model
    def _cron_close_completed(self):
        """Daily housekeeping: drop a worksheet off the live list once every
        job on it is completed or cancelled."""
        for w in self.search([("is_active", "=", True)]):
            if w.job_ids and all(j.stage_id.is_completed or j.stage_id.is_cancelled for j in w.job_ids):
                w.is_active = False


# ═════════════════════════════════════════════════════════════════════════
# Phase O — Reporting Suite
#
# Six pragmatic report wizards in the same shape as DispatchStopReportWizard
# above: filter fields + an action_generate() that queries existing data and
# renders an HTML summary. No new BI subsystem — just aggregation over
# fields that already exist on prema.dispatch.job / prema.dispatch.stop /
# fleet.vehicle.
#
# Fuel efficiency is included: fleet.vehicle already carries GeoTab-synced
# x_avg_km_per_l_last_week / x_avg_l_per_100km_last_week (see
# fleet_vehicle_extension.py), refreshed by a weekly sync cron — so it's
# read directly rather than skipped.
# ═════════════════════════════════════════════════════════════════════════


class DispatchOnTimeReportWizard(models.TransientModel):
    _name = "prema.dispatch.ontime.report.wizard"
    _description = "On-Time % Report"

    driver_id = fields.Many2one("res.partner", string="Driver", domain="[('x_is_driver', '=', True)]")
    vehicle_id = fields.Many2one("fleet.vehicle", string="Truck")
    date_from = fields.Date(default=lambda self: fields.Date.today() - relativedelta(days=30))
    date_to = fields.Date(default=lambda self: fields.Date.today())
    summary_html = fields.Html(readonly=True)

    def action_generate(self):
        self.ensure_one()
        jobs = self.env["prema.dispatch.job"].search(
            _job_domain(self.driver_id, self.vehicle_id, self.date_from, self.date_to), limit=500
        )
        stops = jobs.mapped("stop_ids").filtered(lambda s: s.status == "completed")

        judged = on_time = 0
        late_minutes_total = 0.0
        for s in stops:
            is_judged, ok, late_min = _stop_on_time_info(s)
            if not is_judged:
                continue
            judged += 1
            if ok:
                on_time += 1
            else:
                late_minutes_total += late_min

        late_count = judged - on_time
        pct = (on_time / judged * 100.0) if judged else 0.0
        avg_late = (late_minutes_total / late_count) if late_count else 0.0
        unjudged = len(stops) - judged

        self.summary_html = (
            f"<p><b>{len(jobs)} job(s), {len(stops)} completed stop(s)</b><br/>"
            f"On-time: <b>{pct:.1f}%</b> ({on_time} of {judged} stops with a scheduled window)<br/>"
            f"Late: {late_count} stop(s), avg lateness {_fmt_hm(avg_late)}<br/>"
            f"{unjudged} stop(s) had no time window to judge against (excluded from the %).</p>"
        )
        return _reopen_wizard(self)


class DispatchLocationDwellReportWizard(models.TransientModel):
    _name = "prema.dispatch.location.dwell.report.wizard"
    _description = "Stop Time by Location Report"

    location_id = fields.Many2one("prema.dispatch.location", string="Saved Location")
    date_from = fields.Date(default=lambda self: fields.Date.today() - relativedelta(days=30))
    date_to = fields.Date(default=lambda self: fields.Date.today())
    summary_html = fields.Html(readonly=True)

    def action_generate(self):
        self.ensure_one()
        domain = [
            ("status", "=", "completed"),
            ("actual_arrival_time", "!=", False),
            ("actual_departure_time", "!=", False),
        ]
        if self.location_id:
            domain.append(("saved_location_id", "=", self.location_id.id))
        if self.date_from:
            domain.append(("job_id.scheduled_pickup", ">=", self.date_from))
        if self.date_to:
            domain.append(("job_id.scheduled_pickup", "<=", self.date_to))
        stops = self.env["prema.dispatch.stop"].search(domain, limit=3000)

        by_loc = {}
        for s in stops:
            key = s.saved_location_id.id if s.saved_location_id else f"addr:{s.address}"
            label = s.saved_location_id.name if s.saved_location_id else (s.address or "Unmapped address")
            dwell = (s.actual_departure_time - s.actual_arrival_time).total_seconds() / 60.0
            entry = by_loc.setdefault(key, {"label": label, "count": 0, "total": 0.0, "max": 0.0})
            entry["count"] += 1
            entry["total"] += dwell
            entry["max"] = max(entry["max"], dwell)

        rows = sorted(by_loc.values(), key=lambda e: e["total"] / e["count"], reverse=True)
        parts = [
            f"<p><b>{len(rows)} location(s), {len(stops)} stop(s)</b> — slowest average dwell first.</p>"
            "<table class='table table-sm'><tr><th>Location</th><th>Stops</th>"
            "<th>Avg Dwell</th><th>Max Dwell</th></tr>"
        ]
        for r in rows[:200]:
            avg = r["total"] / r["count"]
            parts.append(
                f"<tr><td>{r['label']}</td><td>{r['count']}</td>"
                f"<td>{_fmt_hm(avg)}</td><td>{_fmt_hm(r['max'])}</td></tr>"
            )
        parts.append("</table>")
        self.summary_html = "".join(parts)
        return _reopen_wizard(self)


class DispatchPerformanceReportWizard(models.TransientModel):
    _name = "prema.dispatch.performance.report.wizard"
    _description = "Driver / Truck Performance Report"

    group_by = fields.Selection(
        [("driver", "Driver"), ("truck", "Truck")], default="driver", required=True
    )
    driver_id = fields.Many2one("res.partner", string="Driver", domain="[('x_is_driver', '=', True)]")
    vehicle_id = fields.Many2one("fleet.vehicle", string="Truck")
    date_from = fields.Date(default=lambda self: fields.Date.today() - relativedelta(days=30))
    date_to = fields.Date(default=lambda self: fields.Date.today())
    summary_html = fields.Html(readonly=True)

    def action_generate(self):
        self.ensure_one()
        jobs = self.env["prema.dispatch.job"].search(
            _job_domain(self.driver_id, self.vehicle_id, self.date_from, self.date_to), limit=1000
        )
        key_field = "driver_id" if self.group_by == "driver" else "vehicle_id"

        groups = {}
        for job in jobs:
            key_rec = job[key_field]
            key = key_rec.id if key_rec else 0
            label = key_rec.name if key_rec else "Unassigned"
            entry = groups.setdefault(key, {"label": label, "jobs": self.env["prema.dispatch.job"]})
            entry["jobs"] |= job

        rows = []
        for entry in groups.values():
            group_jobs = entry["jobs"]
            drive_min, detention_min, stop_count = _job_hours(group_jobs)
            stops = group_jobs.mapped("stop_ids").filtered(lambda s: s.status == "completed")
            judged = on_time = 0
            for s in stops:
                is_judged, ok, _late = _stop_on_time_info(s)
                if is_judged:
                    judged += 1
                    on_time += 1 if ok else 0
            pct = (on_time / judged * 100.0) if judged else 0.0
            job_count = len(group_jobs)
            rows.append((entry["label"], job_count, stop_count, pct, drive_min, detention_min))

        rows.sort(key=lambda r: r[0] or "")
        label_col = "Driver" if self.group_by == "driver" else "Truck"
        parts = [
            f"<table class='table table-sm'><tr><th>{label_col}</th><th>Jobs</th><th>Stops</th>"
            "<th>On-Time %</th><th>Total Drive</th><th>Total Detention</th>"
            "<th>Avg Drive / Job</th><th>Avg Detention / Job</th></tr>"
        ]
        for label, job_count, stop_count, pct, drive_min, detention_min in rows:
            avg_drive = drive_min / job_count if job_count else 0
            avg_det = detention_min / job_count if job_count else 0
            parts.append(
                f"<tr><td>{label}</td><td>{job_count}</td><td>{stop_count}</td>"
                f"<td>{pct:.1f}%</td><td>{_fmt_hm(drive_min)}</td><td>{_fmt_hm(detention_min)}</td>"
                f"<td>{_fmt_hm(avg_drive)}</td><td>{_fmt_hm(avg_det)}</td></tr>"
            )
        parts.append("</table>")
        self.summary_html = "".join(parts)
        return _reopen_wizard(self)


class DispatchLaneProfitabilityReportWizard(models.TransientModel):
    _name = "prema.dispatch.lane.report.wizard"
    _description = "Lane Profitability Report"

    date_from = fields.Date(default=lambda self: fields.Date.today() - relativedelta(days=90))
    date_to = fields.Date(default=lambda self: fields.Date.today())
    summary_html = fields.Html(readonly=True)

    def action_generate(self):
        self.ensure_one()
        domain = []
        if self.date_from:
            domain.append(("scheduled_pickup", ">=", self.date_from))
        if self.date_to:
            domain.append(("scheduled_pickup", "<=", self.date_to))
        jobs = self.env["prema.dispatch.job"].search(domain, limit=1000)

        lanes = {}
        for job in jobs:
            lane_key = (job.pickup_city or "Unknown", job.delivery_cities or "Unknown")
            revenue = job.invoice_id.amount_total if job.invoice_id else 0.0
            cost = job.estimated_cost or 0.0
            entry = lanes.setdefault(
                lane_key, {"jobs": 0, "revenue": 0.0, "cost": 0.0, "distance_km": 0.0}
            )
            entry["jobs"] += 1
            entry["revenue"] += revenue
            entry["cost"] += cost
            entry["distance_km"] += job.estimated_distance_km or 0.0

        rows = []
        for (origin, dest), v in lanes.items():
            margin = v["revenue"] - v["cost"]
            margin_pct = (margin / v["revenue"] * 100.0) if v["revenue"] else 0.0
            rows.append((origin, dest, v["jobs"], v["revenue"], v["cost"], margin, margin_pct, v["distance_km"]))
        rows.sort(key=lambda r: r[3], reverse=True)  # highest revenue first

        parts = [
            f"<p><b>{len(rows)} lane(s), {len(jobs)} job(s)</b><br/>"
            "Cost is the internal Est. Cost field (fuel + driver time) — not a full landed-cost "
            "model, so treat margin as directional, not exact.</p>"
            "<table class='table table-sm'><tr><th>Origin</th><th>Destination</th><th>Jobs</th>"
            "<th>Revenue</th><th>Est. Cost</th><th>Margin</th><th>Margin %</th><th>Avg Distance</th></tr>"
        ]
        for origin, dest, job_count, revenue, cost, margin, margin_pct, distance_km in rows[:200]:
            avg_km = distance_km / job_count if job_count else 0
            parts.append(
                f"<tr><td>{origin}</td><td>{dest}</td><td>{job_count}</td>"
                f"<td>${revenue:,.2f}</td><td>${cost:,.2f}</td><td>${margin:,.2f}</td>"
                f"<td>{margin_pct:.1f}%</td><td>{avg_km:.0f} km</td></tr>"
            )
        parts.append("</table>")
        self.summary_html = "".join(parts)
        return _reopen_wizard(self)


class DispatchFuelEfficiencyReportWizard(models.TransientModel):
    _name = "prema.dispatch.fuel.report.wizard"
    _description = "Fuel Efficiency Report"

    vehicle_id = fields.Many2one("fleet.vehicle", string="Truck")
    summary_html = fields.Html(readonly=True)

    def action_generate(self):
        self.ensure_one()
        domain = []
        if self.vehicle_id:
            domain.append(("id", "=", self.vehicle_id.id))
        vehicles = self.env["fleet.vehicle"].search(domain, limit=300)
        vehicles = vehicles.filtered(
            lambda v: v.x_avg_km_per_l_last_week or v.x_avg_l_per_100km_last_week or v.x_last_fuel_sync_at
        )
        rows = vehicles.sorted(lambda v: v.x_avg_km_per_l_last_week or 0.0)

        if not rows:
            self.summary_html = (
                "<p>No GeoTab-synced fuel efficiency data found on any truck "
                "(fleet.vehicle.x_avg_km_per_l_last_week is empty for all trucks in scope). "
                "This is populated by the weekly Geotab fuel-averages sync cron — confirm it's "
                "enabled under Settings &gt; Technical &gt; GeoTab, and that trucks have fuel-level "
                "telemetry reporting.</p>"
            )
            return _reopen_wizard(self)

        parts = [
            "<p>Sorted worst efficiency first (lowest km/L).</p>"
            "<table class='table table-sm'><tr><th>Truck</th><th>Avg km/L (last week)</th>"
            "<th>Avg L/100km (last week)</th><th>Current Fuel %</th><th>Last Fuel Sync</th></tr>"
        ]
        for v in rows:
            sync_at = _fmt_stop_local(v.x_last_fuel_sync_at, "America/Toronto") if v.x_last_fuel_sync_at else "—"
            parts.append(
                f"<tr><td>{v.name}</td><td>{v.x_avg_km_per_l_last_week:.2f}</td>"
                f"<td>{v.x_avg_l_per_100km_last_week:.2f}</td>"
                f"<td>{v.x_current_fuel_percent:.0f}%</td><td>{sync_at}</td></tr>"
            )
        parts.append("</table>")
        self.summary_html = "".join(parts)
        return _reopen_wizard(self)


class DispatchLateStopReportWizard(models.TransientModel):
    _name = "prema.dispatch.late.stop.report.wizard"
    _description = "Missed / Late Stop Report"

    driver_id = fields.Many2one("res.partner", string="Driver", domain="[('x_is_driver', '=', True)]")
    vehicle_id = fields.Many2one("fleet.vehicle", string="Truck")
    date_from = fields.Date(default=lambda self: fields.Date.today() - relativedelta(days=30))
    date_to = fields.Date(default=lambda self: fields.Date.today())
    summary_html = fields.Html(readonly=True)

    def action_generate(self):
        self.ensure_one()
        jobs = self.env["prema.dispatch.job"].search(
            _job_domain(self.driver_id, self.vehicle_id, self.date_from, self.date_to), limit=500
        )
        late_rows = []
        for job in jobs:
            for s in job.stop_ids.filtered(lambda s: s.status == "completed"):
                is_judged, ok, late_min = _stop_on_time_info(s)
                if is_judged and not ok:
                    late_rows.append((job, s, late_min))
        late_rows.sort(key=lambda r: r[2], reverse=True)

        parts = [
            f"<p><b>{len(late_rows)} late stop(s)</b> across {len(jobs)} job(s) — worst lateness first.</p>"
            "<table class='table table-sm'><tr><th>Job</th><th>Stop</th><th>Address</th>"
            "<th>Scheduled By</th><th>Arrived</th><th>Late By</th></tr>"
        ]
        for job, s, late_min in late_rows[:300]:
            target = s.exact_time or s.deadline_time or s.latest_time or s.scheduled_time
            parts.append(
                f"<tr><td>{job.name}</td><td>{s.name}</td><td>{s.address or ''}</td>"
                f"<td>{_fmt_stop_local(target, s.tz_name)}</td>"
                f"<td>{_fmt_stop_local(s.actual_arrival_time, s.tz_name)}</td>"
                f"<td>{_fmt_hm(late_min)}</td></tr>"
            )
        parts.append("</table>")
        self.summary_html = "".join(parts)
        return _reopen_wizard(self)


class DispatchPodAgingReportWizard(models.TransientModel):
    _name = "prema.dispatch.pod.aging.report.wizard"
    _description = "POD Aging Report"

    driver_id = fields.Many2one("res.partner", string="Driver", domain="[('x_is_driver', '=', True)]")
    vehicle_id = fields.Many2one("fleet.vehicle", string="Truck")
    summary_html = fields.Html(readonly=True)

    def action_generate(self):
        self.ensure_one()
        domain = [
            ("pod_required", "=", True),
            ("status", "=", "completed"),
            ("pod_uploaded", "=", False),
        ]
        if self.driver_id:
            domain.append(("job_id.driver_id", "=", self.driver_id.id))
        if self.vehicle_id:
            domain.append(("job_id.vehicle_id", "=", self.vehicle_id.id))
        stops = self.env["prema.dispatch.stop"].search(domain, limit=500)

        now = fields.Datetime.now()
        rows = []
        for s in stops:
            completed_at = s.actual_departure_time or s.actual_arrival_time
            if not completed_at:
                continue
            age_days = (now - completed_at).total_seconds() / 86400.0
            rows.append((s, completed_at, age_days))
        rows.sort(key=lambda r: r[2], reverse=True)

        parts = [
            f"<p><b>{len(rows)} completed stop(s) missing POD evidence</b> — oldest first.</p>"
            "<table class='table table-sm'><tr><th>Job</th><th>Stop</th><th>Driver</th>"
            "<th>Truck</th><th>Completed</th><th>Age</th></tr>"
        ]
        for s, completed_at, age_days in rows[:300]:
            parts.append(
                f"<tr><td>{s.job_id.name}</td><td>{s.name}</td>"
                f"<td>{s.job_id.driver_id.name or ''}</td><td>{s.job_id.vehicle_id.name or ''}</td>"
                f"<td>{_fmt_stop_local(completed_at, s.tz_name)}</td><td>{age_days:.1f} day(s)</td></tr>"
            )
        parts.append("</table>")
        self.summary_html = "".join(parts)
        return _reopen_wizard(self)

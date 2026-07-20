from collections import defaultdict

from odoo import api, fields, models


class PremaDispatchConsolidation(models.Model):
    _name = "prema.dispatch.consolidation"
    _description = "LTL Consolidation Suggestion"
    _inherit = ["mail.thread"]
    _order = "create_date desc, id desc"

    name = fields.Char(compute="_compute_name", store=True)
    status = fields.Selection([
        ("suggested",  "Suggested"),
        ("accepted",   "Consolidating"),
        ("completed",  "Completed"),
        ("dismissed",  "Dismissed"),
    ], default="suggested", tracking=True, required=True)

    pickup_city = fields.Char(readonly=True)
    delivery_cities = fields.Char(readonly=True)
    job_ids = fields.Many2many(
        "prema.dispatch.job",
        "dispatch_consolidation_job_rel",
        "consolidation_id", "job_id",
        string="Jobs to Consolidate",
    )
    job_count = fields.Integer(compute="_compute_job_count")
    total_skids = fields.Integer(compute="_compute_totals", store=True)
    total_weight_lbs = fields.Float(
        compute="_compute_totals", store=True, digits=(10, 1)
    )
    suggested_by = fields.Many2one(
        "res.users", default=lambda self: self.env.user, readonly=True
    )
    notes = fields.Text()

    @api.depends("pickup_city", "delivery_cities")
    def _compute_name(self):
        for rec in self:
            rec.name = (
                f"{rec.pickup_city or '?'} → {rec.delivery_cities or '?'}"
            )

    @api.depends("job_ids")
    def _compute_job_count(self):
        for rec in self:
            rec.job_count = len(rec.job_ids)

    @api.depends("job_ids.total_skids", "job_ids.total_weight_lbs")
    def _compute_totals(self):
        for rec in self:
            rec.total_skids = sum(j.total_skids for j in rec.job_ids)
            rec.total_weight_lbs = sum(j.total_weight_lbs for j in rec.job_ids)

    def action_accept(self):
        self.write({"status": "accepted"})
        self.message_post(body="Consolidation accepted — plan these jobs together.")

    def action_dismiss(self):
        self.write({"status": "dismissed"})

    def action_complete(self):
        self.write({"status": "completed"})

    @api.model
    def suggest_consolidations(self):
        """
        Find LTL booking-phase jobs to consolidate.
        Pass 1: same pickup city + within 3 days (original logic).
        Pass 2: same corridor tag + same reefer requirement + within 3 days
                (catches Brampton–Ottawa + Woodbridge–Belleville type matches).
        """
        ltl_jobs = self.env["prema.dispatch.job"].search([
            ("service_type", "=", "ltl"),
            ("stage_id.is_booking_phase", "=", True),
            ("vehicle_id", "=", False),
        ])
        if len(ltl_jobs) < 2:
            return 0

        today = fields.Date.today()
        created = 0

        def _cluster_by_date(jobs):
            """Group a sorted list of jobs into clusters within 3 days of the anchor."""
            groups = []
            if not jobs:
                return groups
            current = [jobs[0]]
            for j in jobs[1:]:
                anchor = current[0].requested_delivery_date or today
                this = j.requested_delivery_date or today
                if abs((this - anchor).days) <= 3:
                    current.append(j)
                else:
                    if len(current) >= 2:
                        groups.append(current)
                    current = [j]
            if len(current) >= 2:
                groups.append(current)
            return groups

        def _already_exists(group_set):
            existing = self.search([("status", "in", ["suggested", "accepted"])])
            return any(frozenset(rec.job_ids.ids) == group_set for rec in existing)

        def _create_suggestion(group, pickup_label):
            group_set = frozenset(j.id for j in group)
            if _already_exists(group_set):
                return 0
            all_cities = set()
            for j in group:
                if j.delivery_cities:
                    all_cities.update(c.strip() for c in j.delivery_cities.split(","))
            self.create({
                "pickup_city": pickup_label,
                "delivery_cities": ", ".join(sorted(all_cities)),
                "job_ids": [(6, 0, [j.id for j in group])],
            })
            return 1

        # ── Pass 1: same pickup city ───────────────────────────────────
        by_pickup = defaultdict(list)
        for job in ltl_jobs.filtered("pickup_city"):
            by_pickup[(job.pickup_city or "").strip().lower()].append(job)

        for pickup_key, jobs in by_pickup.items():
            if len(jobs) < 2:
                continue
            sorted_jobs = sorted(jobs, key=lambda j: j.requested_delivery_date or today)
            for group in _cluster_by_date(sorted_jobs):
                # Equipment must match: don't mix reefer + dry
                reefer_jobs = [j for j in group if j.requires_reefer]
                dry_jobs = [j for j in group if not j.requires_reefer]
                for subgroup in [reefer_jobs, dry_jobs]:
                    if len(subgroup) >= 2:
                        created += _create_suggestion(subgroup, pickup_key.title())

        # ── Pass 2: same corridor tag (catches nearby-city eastbound/westbound lanes) ──
        corridor_jobs = ltl_jobs.filtered(
            lambda j: j.corridor_tag and j.corridor_tag not in ("", "LOCAL")
        )
        # Bucket by (corridor_tag, requires_reefer)
        by_corridor = defaultdict(list)
        for job in corridor_jobs:
            key = (job.corridor_tag, bool(job.requires_reefer))
            by_corridor[key].append(job)

        for (corridor, is_reefer), jobs in by_corridor.items():
            if len(jobs) < 2:
                continue
            # Only include jobs whose pickup cities differ (same-city already covered in pass 1)
            multi_city = [
                j for j in jobs
                if jobs.count(j) == 1  # no duplicates
            ]
            seen_cities = set()
            diverse = []
            for j in multi_city:
                city = (j.pickup_city or "").strip().lower()
                if city not in seen_cities:
                    seen_cities.add(city)
                    diverse.append(j)
                else:
                    diverse.append(j)

            # Only proceed if at least 2 different pickup cities are represented
            pickup_cities = {(j.pickup_city or "").strip().lower() for j in jobs}
            if len(pickup_cities) < 2:
                continue

            sorted_jobs = sorted(jobs, key=lambda j: j.requested_delivery_date or today)
            for group in _cluster_by_date(sorted_jobs):
                group_cities = {(j.pickup_city or "").strip().lower() for j in group}
                if len(group_cities) < 2:
                    continue  # skip if same city (already handled in pass 1)
                label = f"{corridor} corridor ({'Reefer' if is_reefer else 'Dry'})"
                created += _create_suggestion(group, label)

        return created

    def action_open_jobs(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Jobs in Consolidation",
            "res_model": "prema.dispatch.job",
            "view_mode": "list,form",
            "domain": [("id", "in", self.job_ids.ids)],
        }

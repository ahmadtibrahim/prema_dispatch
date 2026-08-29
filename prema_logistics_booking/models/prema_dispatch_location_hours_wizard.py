"""Bulk Facility Hours Editor — dispatcher tool to apply ONE weekly hours
template across many canonical facilities at once, with an integrity
preview (facilities with no hours rows at all plan as CLOSED — the
planner's missing-hours semantics — so the preview surfaces them first).

All writes are stamped with source='wizard' + changed_by/changed_at for
the hours audit trail."""

from odoo import api, fields, models

_DAY_FIELDS = [
    ("apply_monday", "0"),
    ("apply_tuesday", "1"),
    ("apply_wednesday", "2"),
    ("apply_thursday", "3"),
    ("apply_friday", "4"),
    ("apply_saturday", "5"),
    ("apply_sunday", "6"),
]


class PremaDispatchLocationHoursWizard(models.TransientModel):
    _name = "prema.dispatch.location.hours.wizard"
    _description = "Bulk Facility Hours Editor"

    facility_ids = fields.Many2many(
        "prema.dispatch.location",
        "prema_dispatch_loc_hours_wizard_facility_rel",
        "wizard_id", "facility_id",
        string="Facilities", required=True)
    service_scope = fields.Selection([
        ("general", "General Hours"),
        ("pickup", "Pickup Hours"),
        ("receiving", "Receiving Hours"),
        ("shipping", "Shipping Hours"),
    ], default="general", required=True)
    apply_monday = fields.Boolean(string="Mon", default=True)
    apply_tuesday = fields.Boolean(string="Tue", default=True)
    apply_wednesday = fields.Boolean(string="Wed", default=True)
    apply_thursday = fields.Boolean(string="Thu", default=True)
    apply_friday = fields.Boolean(string="Fri", default=True)
    apply_saturday = fields.Boolean(string="Sat", default=False)
    apply_sunday = fields.Boolean(string="Sun", default=False)
    day_status = fields.Selection([
        ("closed", "Closed"),
        ("open_24h", "Open 24 Hours"),
        ("custom", "Custom Hours"),
    ], default="open_24h", required=True)
    open_time = fields.Float(
        string="Open", help="Hours as float (e.g. 8.5 = 08:30) — custom only")
    close_time = fields.Float(
        string="Close", help="Hours as float (e.g. 17.0 = 17:00) — custom only")

    preview = fields.Text(string="Integrity Preview", compute="_compute_preview")

    @api.depends("facility_ids", "service_scope",
                 "apply_monday", "apply_tuesday", "apply_wednesday",
                 "apply_thursday", "apply_friday", "apply_saturday",
                 "apply_sunday", "day_status", "open_time", "close_time")
    def _compute_preview(self):
        Hours = self.env["prema.dispatch.location.hours"]
        for wiz in self:
            lines = []
            for facility in wiz.facility_ids:
                rows = Hours.search([
                    ("facility_id", "=", facility.id), ("active", "=", True)])
                if not rows:
                    lines.append(
                        "⚠ %s — NO hours rows: plans as CLOSED every day"
                        % (facility.name or facility.address or facility.id))
                    continue
                by_day = {r.day_of_week: r for r in rows}
                missing = [d for d in range(7) if str(d) not in by_day]
                if missing:
                    lines.append(
                        "• %s — missing day rows (planned as Closed): %s"
                        % (facility.name or facility.address or facility.id,
                           ", ".join("Mon Tue Wed Thu Fri Sat Sun".split()[d]
                                     for d in missing)))
                split = rows.filtered(lambda r: r.sequence and r.sequence != 10)
                if split:
                    lines.append(
                        "• %s — %d split-window row(s) (merged into one open span)"
                        % (facility.name or facility.address or facility.id, len(split)))
                overnight = rows.filtered(
                    lambda r: r.status == "custom"
                    and r.open_time and r.close_time
                    and r.open_time > r.close_time)
                if overnight:
                    lines.append(
                        "• %s — %d overnight row(s) (not representable; "
                        "use Open 24 Hours instead)"
                        % (facility.name or facility.address or facility.id, len(overnight)))
            selected = sum(getattr(wiz, f[0]) for f in _DAY_FIELDS)
            total_rows = len(wiz.facility_ids) * selected
            head = "Applying %s / %s to %d facility(ies) × %d day(s) = %d row(s)." % (
                wiz.day_status,
                ("%02d:%02d–%02d:%02d" % (int(wiz.open_time or 0),
                                          int(round((wiz.open_time % 1) * 60)) % 60,
                                          int(wiz.close_time or 0),
                                          int(round((wiz.close_time % 1) * 60)) % 60)
                 if wiz.day_status == "custom" else "—"),
                len(wiz.facility_ids), selected, total_rows)
            wiz.preview = "\n".join([head] + (lines or ["✓ No integrity issues."]))

    def apply_hours(self):
        """Replace the selected scope+day rows on every facility with the
        template (old rows deactivated, never deleted), stamping the audit
        trail, then return to the Master Facilities list filtered to what
        was just edited."""
        self.ensure_one()
        if not self.facility_ids:
            raise ValueError("Select at least one facility.")
        selected_days = [day for field, day in _DAY_FIELDS if getattr(self, field)]
        if not selected_days:
            raise ValueError("Select at least one day to apply.")
        if self.day_status == "custom" and not self.open_time and not self.close_time:
            raise ValueError("Custom hours need an open and close time.")

        Hours = self.env["prema.dispatch.location.hours"]
        now = fields.Datetime.now()
        user = self.env.user
        for facility in self.facility_ids:
            old = Hours.search([
                ("facility_id", "=", facility.id),
                ("service_scope", "=", self.service_scope),
                ("day_of_week", "in", selected_days),
                ("active", "=", True),
            ])
            if old:
                old.write({"active": False})
            for day in selected_days:
                status = self.day_status
                open_t, close_t = 0.0, 24.0
                if status == "custom":
                    open_t = self.open_time or 0.0
                    close_t = self.close_time or 24.0
                elif status == "closed":
                    open_t, close_t = 0.0, 0.0
                Hours.create({
                    "facility_id": facility.id,
                    "day_of_week": day,
                    "service_scope": self.service_scope,
                    "status": status,
                    "open_time": open_t,
                    "close_time": close_t,
                    "sequence": 10,
                    "active": True,
                    "source": "wizard",
                    "changed_by": user.id,
                    "changed_at": now,
                })
        return {
            "type": "ir.actions.act_window",
            "res_model": "prema.dispatch.location",
            "view_mode": "tree,form",
            "domain": [("id", "in", self.facility_ids.ids)],
            "name": "Master Facilities — hours updated",
            "context": dict(self.env.context, search_default_hours=True),
        }

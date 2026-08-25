"""Customer-access relation for shared facilities.

Canonical facility authority is prema.dispatch.location (one record per
physical facility). What belongs to an individual CUSTOMER of that facility
— portal visibility, pickup/delivery capability, defaults, alias, private
contact and instructions — lives here, never on the shared master. This is
what lets many customers reuse one facility without ever overwriting the
master's partner_id or leaking one customer's data to another.

logistics.saved.location remains the customer's own saved-location record
(their convenience copy with their own contact/instructions); this model is
the join between that customer and the canonical facility.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class LogisticsLocationCustomerAccess(models.Model):
    _name = "logistics.location.customer.access"
    _description = "Customer Access to Shared Facility"
    _rec_name = "display_name"
    _order = "facility_id, commercial_partner_id"

    display_name = fields.Char(
        compute="_compute_display_name", store=False,
        help="Facility — customer (alias), e.g. 'Foodland #42 — Acme Co (HQ)'.",
    )

    facility_id = fields.Many2one(
        "prema.dispatch.location", string="Facility",
        required=True, ondelete="cascade", index=True,
    )
    commercial_partner_id = fields.Many2one(
        "res.partner", string="Customer",
        required=True, ondelete="cascade", index=True,
    )
    active = fields.Boolean(default=True)

    # What this customer may use the facility for (master stop_type remains
    # the global authority; these gates are per-customer).
    portal_enabled = fields.Boolean(
        string="Portal Enabled", default=True,
        help="When on, this facility appears in this customer's portal "
             "Saved Location autocomplete.",
    )
    can_pickup = fields.Boolean(string="Can Pickup", default=True)
    can_delivery = fields.Boolean(string="Can Delivery", default=True)
    is_default_pickup = fields.Boolean(string="Default Pickup")
    is_default_delivery = fields.Boolean(string="Default Delivery")

    # Per-customer identity and private data — NEVER on the shared master.
    customer_alias = fields.Char(
        string="Customer Alias",
        help="This customer's name for the facility, e.g. 'HQ warehouse'.",
    )
    customer_reference = fields.Char(string="Customer Reference")
    contact_name = fields.Char(string="Contact Name")
    contact_phone = fields.Char(string="Contact Phone")
    contact_email = fields.Char(string="Contact Email")
    pickup_instructions = fields.Text(string="Pickup Instructions")
    delivery_instructions = fields.Text(string="Delivery Instructions")
    # Per-customer operational preferences (SAVED LOCATION CONSOLIDATION
    # §5 — defaults/contacts/instructions land on the access row, never on
    # the shared facility master).
    appointment_required = fields.Boolean(string="Appointment Required")
    forklift_available = fields.Boolean(string="Forklift Available")
    opening_hours = fields.Char(
        string="Opening Hours (free text)",
        help="Customer-facing note (e.g. 'Mon-Fri 8:00-17:00'). The "
             "scheduling authority is the canonical facility hours table.",
    )
    timezone = fields.Char(default="America/Toronto")

    # Read of the access row in the booking UI (used to order the portal
    # selection lists the way the legacy saved-location list was ordered).
    last_used_date = fields.Datetime(string="Last Used")

    # DEMO TEST SET (§7-§8): rows created for the PREMA DISPATCH DEMO
    # customer's 9-route manual test matrix. is_demo is the single
    # selector for the "Purge Demo Test Data" action (§15) — real
    # customer flows never set it.
    is_demo = fields.Boolean(string="Demo / Test", index=True, default=False)

    # ── Template-compatible computed proxies ───────────────────────────
    # The booking portal consumed logistics.saved.location fields
    # (name, street, city, state_id.code, latitude…). New portal data is
    # access rows: PHYSICAL fields resolve from the canonical facility,
    # PRIVATE fields resolve from this row. The proxy fields below keep
    # every portal template and the step-1/2/3 resolution code working on
    # access records without touching their field names.
    name = fields.Char(compute="_compute_location_proxies", string="Location Name")
    chain_name = fields.Char(compute="_compute_location_proxies")
    business_name = fields.Char(compute="_compute_location_proxies")
    branch_name = fields.Char(compute="_compute_location_proxies")
    store_number = fields.Char(compute="_compute_location_proxies")
    street = fields.Char(compute="_compute_location_proxies")
    street2 = fields.Char(compute="_compute_location_proxies")
    unit = fields.Char(compute="_compute_location_proxies")
    city = fields.Char(compute="_compute_location_proxies")
    postal_code = fields.Char(compute="_compute_location_proxies")
    state_id = fields.Many2one("res.country.state", compute="_compute_location_proxies")
    country_id = fields.Many2one("res.country", compute="_compute_location_proxies")
    google_place_id = fields.Char(compute="_compute_location_proxies")
    formatted_address = fields.Char(compute="_compute_location_proxies")
    latitude = fields.Float(compute="_compute_location_proxies", digits=(10, 6))
    longitude = fields.Float(compute="_compute_location_proxies", digits=(10, 6))
    location_type = fields.Char(compute="_compute_location_proxies")
    dock_info = fields.Char(compute="_compute_location_proxies")
    liftgate_required = fields.Boolean(compute="_compute_location_proxies")
    company_name = fields.Char(compute="_compute_location_proxies")
    region_match_result = fields.Selection(
        [("SCHEDULED_MATCH", "Scheduled Network"),
         ("MANUAL_QUOTE", "Manual Quote Required"),
         ("NETWORK_DISABLED", "Automatic Booking Unavailable"),
         ("AMBIGUOUS", "Manual Region Review Required")],
        compute="_compute_region_match_result", store=False,
        help="Display-only badge for the portal list. RETIRED in 18.0.13.25.0 "
             "(SAVED LOCATION CONSOLIDATION — the legacy saved-location "
             "model was dropped); always False.",
    )

    @api.depends("facility_id")
    def _compute_region_match_result(self):
        for rec in self:
            rec.region_match_result = False

    @api.depends("facility_id", "customer_alias", "can_pickup", "can_delivery")
    def _compute_location_proxies(self):
        State = self.env["res.country.state"]
        for rec in self:
            fac = rec.facility_id
            rec.name = rec.customer_alias or fac.name or fac.chain_name or ""
            rec.chain_name = fac.chain_name or ""
            rec.business_name = fac.business_name or ""
            rec.branch_name = fac.branch_name or ""
            rec.store_number = fac.location_number or ""
            rec.street = fac.street or ""
            rec.street2 = fac.street2 or ""
            rec.unit = fac.unit or ""
            rec.city = fac.city or ""
            rec.postal_code = fac.postal_code or ""
            state = State.search(
                [("code", "=ilike", fac.province_code or "")], limit=1,
            ) if fac.province_code else self.env["res.country.state"]
            rec.state_id = state.id if state else False
            rec.country_id = fac.country_id.id if fac.country_id else False
            rec.google_place_id = fac.google_place_id or ""
            rec.formatted_address = fac.address or fac.street or ""
            rec.latitude = fac.pin_lat or 0.0
            rec.longitude = fac.pin_lng or 0.0
            rec.location_type = (
                "both" if (rec.can_pickup and rec.can_delivery)
                else ("pickup" if rec.can_pickup
                      else ("delivery" if rec.can_delivery else "both"))
            )
            rec.dock_info = fac.dock_door or ""
            rec.liftgate_required = fac.liftgate_required
            rec.company_name = fac.business_name or fac.chain_name or ""

    _sql_constraints = [
        ("unique_facility_partner",
         "UNIQUE(facility_id, commercial_partner_id)",
         "This customer already has access to this facility."),
    ]

    @api.depends("facility_id", "commercial_partner_id", "customer_alias")
    def _compute_display_name(self):
        for rec in self:
            facility = rec.facility_id.name or rec.facility_id.chain_name or ""
            customer = rec.commercial_partner_id.name or ""
            alias = rec.customer_alias or ""
            parts = [p for p in (facility, customer, f"({alias})" if alias else "")]
            rec.display_name = " — ".join(parts) or f"Access #{rec.id}"

    @api.model
    def ensure_access(self, facility, partner, **vals):
        """Get-or-create the access relation for (facility, partner).

        Never duplicates: the UNIQUE constraint guarantees one row per
        customer/facility pair, so customers reuse the canonical facility
        instead of cloning it. The search runs with active_test=False so a
        customer-archived row is FOUND and resurrected (a re-add after
        archive would otherwise hit the UNIQUE constraint and crash with
        "Location already exists" instead of reusing).
        """
        facility_id = facility.id if hasattr(facility, "id") else int(facility or 0)
        partner_id = partner.id if hasattr(partner, "id") else int(partner or 0)
        if not facility_id or not partner_id:
            return self.browse()
        access = self.with_context(active_test=False).search([
            ("facility_id", "=", facility_id),
            ("commercial_partner_id", "=", partner_id),
        ], limit=1)
        if not access:
            access = self.create({
                "facility_id": facility_id,
                "commercial_partner_id": partner_id,
            })
        if vals:
            access.write(vals)
        return access

    @api.model
    def action_purge_demo_data(self):
        """TEST DATA CLEANUP (§15) — remove the demo test set.

        Deletes the access rows with ``is_demo=True`` and the facilities
        that were created FOR the demo set (``prema.dispatch.location.
        is_demo=True``). Never auto-runs — the demo locations stay
        available for manual portal testing until an explicit cleanup
        instruction.

        Guards:
          - refuses when ANY demo facility is referenced by live data
            (booking stops, pricing-session stops, dispatch stops/jobs,
            hubs, corridor stops);
          - never deletes a demo facility that gained a NON-demo access
            row (a real customer started using it — the facility is kept,
            only its demo access rows are removed);
          - the PREMA DISPATCH DEMO CUSTOMER account itself is kept
            (it is the reusable manual-test login).
        """
        DispatchLoc = self.env["prema.dispatch.location"].sudo()
        demo_access = self.sudo().search([("is_demo", "=", True)])
        demo_fac_ids = demo_access.facility_id.ids
        demo_facilities = DispatchLoc.search([("is_demo", "=", True)])

        if not demo_fac_ids and not demo_facilities:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "No demo data",
                    "message": "No demo access rows or demo facilities exist.",
                    "type": "info",
                    "sticky": False,
                },
            }

        # Guard 1 — live-data references on any demo facility.
        ref_checks = [
            ("logistics.booking.stop", "saved_location_id"),
            ("logistics.pricing.session.stop", "facility_id"),
            ("prema.dispatch.stop", "saved_location_id"),
            ("prema.dispatch.job", "pickup_saved_location_id"),
            ("logistics.hub", "saved_location_id"),
            ("logistics.corridor.stop", "saved_location_id"),
        ]
        for model, fk in ref_checks:
            n = self.env[model].sudo().search_count([(fk, "in", demo_fac_ids)])
            if n:
                raise UserError(
                    _("Cannot purge demo data: %d %s record(s) still reference a "
                      "demo facility (field %s). Clear those first.") % (n, model, fk))

        # Guard 2 — a real customer using a demo facility keeps it.
        real_customers = self.sudo().search_count([
            ("facility_id", "in", demo_fac_ids),
            ("is_demo", "=", False),
            ("active", "=", True),
        ])
        removed_access = len(demo_access)
        removed_facilities = 0
        demo_access.unlink()
        if not real_customers:
            removed_facilities = len(demo_facilities)
            demo_facilities.unlink()

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Demo data purged",
                "message": ("Removed %d demo access row(s)%s." % (
                    removed_access,
                    " and %d demo facilities" % removed_facilities
                    if removed_facilities else
                    " (demo facilities kept — a real customer uses one)")),
                "type": "success",
                "sticky": False,
            },
        }

    def _get_effective_coordinates(self):
        """(lat, lng) from the canonical facility pins — the same contract
        logistics.saved.location._get_effective_coordinates() exposes, so
        the portal resolution code treats access rows and legacy saved
        rows interchangeably."""
        return (self.facility_id.pin_lat or 0.0, self.facility_id.pin_lng or 0.0)

    def mark_used(self):
        """Bump last_used_date when the customer picks this location in a
        booking (mirror of logistics.saved.location.mark_used)."""
        self.write({"last_used_date": fields.Datetime.now()})


class PremaDispatchLocationExtension(models.Model):
    """Extension fields on the canonical facility authority.

    Read-only aggregates for the Locations menu columns; the authoritative
    data always lives on the child records (access relations, saved
    locations, dispatch visit samples).
    """

    _inherit = "prema.dispatch.location"

    customer_access_ids = fields.One2many(
        "logistics.location.customer.access", "facility_id",
        string="Customers Using Location",
        help="Which customers may use this facility and their per-customer "
             "contact / instructions. Customer-specific data never lives on "
             "the shared master itself.",
    )
    customer_access_count = fields.Integer(
        string="Customers Using", compute="_compute_location_aggregates",
        store=False,
    )
    facility_hours_ids = fields.One2many(
        "prema.dispatch.location.hours", "facility_id",
        string="Facility Hours",
        help="Structured weekly operating hours on the CANONICAL facility — "
             "the scheduling authority for this physical facility. Migrated "
             "once from the legacy saved-location hours; identical sources "
             "merge, conflicting sources raise hours_review_required.",
    )
    hours_review_required = fields.Boolean(
        string="Hours Review Required", default=False,
        help="True when legacy hours sources for this facility conflicted "
             "during consolidation — a dispatcher must confirm the schedule "
             "before it is treated as authoritative. Never auto-guessed.",
    )
    legacy_hours_source = fields.Char(
        string="Legacy Hours Source",
        help="Names/ids of the legacy saved location(s) whose hours were "
             "migrated onto this facility (preserved when a review is "
             "required).",
    )
    facility_hours_summary = fields.Char(
        string="Facility Hours", compute="_compute_location_aggregates",
        store=False,
        help="Compact weekly schedule from the canonical facility hours "
             "(prema.dispatch.location.hours), falling back to linked "
             "saved locations during the legacy transition.",
    )
    region_label = fields.Char(
        string="Region", compute="_compute_location_aggregates", store=False,
        help="Detected logistics region of linked saved locations, falling "
             "back to a province-group label.",
    )

    _PROVINCE_REGION_LABELS = {
        "QC": "Quebec",
        "ON": "Ontario",
        "MB": "West", "SK": "West", "AB": "West", "BC": "West",
        "NS": "Atlantic", "NB": "Atlantic", "PE": "Atlantic",
        "NL": "Atlantic", "NT": "Territories", "YT": "Territories",
        "NU": "Territories",
    }

    @api.depends("customer_access_ids", "google_verified",
                 "facility_hours_ids", "facility_hours_ids.status",
                 "facility_hours_ids.day_of_week",
                 "facility_hours_ids.service_scope",
                 "facility_hours_ids.open_time", "facility_hours_ids.close_time")
    def _compute_location_aggregates(self):
        from .prema_dispatch_location_hours import hours_summary_from_rows
        for loc in self:
            loc.customer_access_count = len(loc.customer_access_ids)

            # Facility hours: canonical hours on the facility itself are the
            # only authority (SAVED LOCATION CONSOLIDATION 18.0.13.25.0 —
            # the legacy saved-location schedule fallback was retired).
            canonical = loc.facility_hours_ids.filtered(
                lambda r: r.active and r.service_scope == "general")
            if canonical:
                loc.facility_hours_summary = hours_summary_from_rows(canonical)
            else:
                loc.facility_hours_summary = ""

            # Region: prefer a detected logistics region from linked saved
            # locations, fall back to province-group label.
            region = ""
            for saved in linked:
                if saved.detected_region_id:
                    region = saved.detected_region_id.name or ""
                    break
            if not region and loc.province_code:
                region = self._PROVINCE_REGION_LABELS.get(
                    loc.province_code.upper(), "")
            loc.region_label = region

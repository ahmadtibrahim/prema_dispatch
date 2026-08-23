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
        instead of cloning it.
        """
        facility_id = facility.id if hasattr(facility, "id") else int(facility or 0)
        partner_id = partner.id if hasattr(partner, "id") else int(partner or 0)
        if not facility_id or not partner_id:
            return self.browse()
        access = self.search([
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
    facility_hours_summary = fields.Char(
        string="Facility Hours", compute="_compute_location_aggregates",
        store=False,
        help="Best-effort weekly schedule drawn from the linked saved "
             "locations' structured operating hours (the facility master "
             "holds no hours of its own).",
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

    @api.depends("customer_access_ids", "google_verified")
    def _compute_location_aggregates(self):
        SavedLocation = self.env["logistics.saved.location"]
        for loc in self:
            loc.customer_access_count = len(loc.customer_access_ids)

            # Facility hours: first linked saved location that has a
            # structured weekly schedule (open_24h / custom / closed rows).
            linked = SavedLocation.search(
                [("dispatch_location_id", "=", loc.id),
                 ("active", "=", True)], limit=5)
            summary = ""
            for saved in linked:
                if saved.hours_status == "configured":
                    summary = saved.hours_summary
                    break
            loc.facility_hours_summary = summary or ""

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

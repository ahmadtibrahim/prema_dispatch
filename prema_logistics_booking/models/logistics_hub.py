"""Multi-Hub Model — canonical transfer hub authority.

Supports multiple active hubs. Only one default per company.
Used by routing, pricing, and the Where We Go map.
"""
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class LogisticsHub(models.Model):
    _name = "logistics.hub"
    _description = "Transfer Hub"
    _order = "name"

    name = fields.Char(string="Internal Name", required=True,
                       help="e.g. Mississauga Hub")
    public_name = fields.Char(string="Public Name", required=True,
                              help="e.g. Transit Mississauga, ON")
    code = fields.Char(string="Code", help="Short code: e.g. YYZ-HUB")

    # ── Location ──────────────────────────────────────────────────────
    saved_location_id = fields.Many2one(
        "prema.dispatch.location", string="Saved Location",
        help="Canonical saved location for this hub.")
    formatted_address = fields.Char(string="Address")
    google_place_id = fields.Char(string="Google Place ID")
    latitude = fields.Float(digits=(10, 6))
    longitude = fields.Float(digits=(10, 6))
    timezone = fields.Char(default="America/Toronto")

    # ── Operations ────────────────────────────────────────────────────
    operating_hours = fields.Char(string="Operating Hours",
                                  default="Mon-Fri 06:00-18:00")
    dry_storage_available = fields.Boolean(default=True)
    chilled_storage_available = fields.Boolean(default=False)
    frozen_storage_available = fields.Boolean(default=False)
    cross_dock_enabled = fields.Boolean(default=True)
    dock_count = fields.Integer(default=1)
    liftgate_access = fields.Boolean(default=True)
    transfer_cutoff_time = fields.Float(
        default=16.0,
        help="Latest arrival time (hours) for same-day transfer to next departure.")

    # ── Contacts ──────────────────────────────────────────────────────
    contact_name = fields.Char()
    contact_phone = fields.Char()
    contact_email = fields.Char()

    # ── Regions ───────────────────────────────────────────────────────
    canonical_region_id = fields.Many2one(
        "logistics.region", string="Canonical Region",
        index=True,
        help="The single physical region this hub is located in. "
             "Used by RouteResolver for hub-transfer routing. "
             "Only YYZ-HUB → R1 is currently confirmed.")
    supported_region_ids = fields.Many2many(
        "logistics.region", "logistics_hub_region_rel",
        "hub_id", "region_id", string="Supported Regions",
        help="Regions this hub can serve for feeder/linehaul connections.")

    # ── Status ────────────────────────────────────────────────────────
    active = fields.Boolean(default=True)
    customer_visible = fields.Boolean(default=True,
        help="Show on public map and customer-facing pages.")
    is_default = fields.Boolean(
        string="Default Hub", default=False,
        help="Only one hub may be the company default at a time.")

    # ── Description ───────────────────────────────────────────────────
    internal_notes = fields.Text(string="Internal Notes")
    public_description = fields.Text(
        string="Public Description",
        help="Shown to customers on map and service pages.")

    company_id = fields.Many2one(
        "res.company", default=lambda self: self.env.company)

    _sql_constraints = [
        ("code_uniq", "unique(code)", "Hub code must be unique."),
    ]

    @api.constrains("is_default")
    def _check_one_default(self):
        for rec in self:
            if rec.is_default:
                others = self.search([
                    ("is_default", "=", True),
                    ("id", "!=", rec.id),
                    ("company_id", "=", rec.company_id.id),
                ])
                others.write({"is_default": False})

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._check_one_default()
        return records

    def write(self, vals):
        result = super().write(vals)
        if "is_default" in vals:
            self._check_one_default()
        return result

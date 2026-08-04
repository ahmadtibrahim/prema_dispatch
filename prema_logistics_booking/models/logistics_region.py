from odoo import _, api, fields, models
from odoo.exceptions import AccessError


def _require_dispatch_staff(env):
    if not env.user.has_group("prema_dispatch.group_dispatcher") and \
       not env.user.has_group("prema_dispatch.group_dispatch_manager"):
        raise AccessError(_("Only dispatchers and logistics managers can view the network map."))


class LogisticsRegion(models.Model):
    _name = "logistics.region"
    _description = "Service Region — pricing, routing, and map authority"
    _order = "display_sequence, code"

    code = fields.Char(required=True, index=True)
    name = fields.Char(required=True)
    display_number = fields.Integer(string="Display Number", help="Number on map marker")
    main_city = fields.Char(string="Main City")
    hub_name = fields.Char(string="Hub")

    rate_per_km = fields.Float(string="Rate per km", default=3.00)

    # ── Map fields ────────────────────────────────────────────────────
    marker_latitude = fields.Float(string="Marker Latitude", digits=(10, 6))
    marker_longitude = fields.Float(string="Marker Longitude", digits=(10, 6))
    polygon_geojson = fields.Text(string="Polygon GeoJSON")
    public_description = fields.Text(string="Public Description")
    default_hub_id = fields.Many2one("logistics.hub", string="Default Hub")

    # ── Status ────────────────────────────────────────────────────────
    customer_visible = fields.Boolean(default=True, string="Customer Visible")
    phase = fields.Integer(default=1)
    display_sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    # ── Postal Coverage ──────────────────────────────────────────────
    fsa_ids = fields.One2many("logistics.fsa", "region_id", string="FSAs")
    active_fsa_count = fields.Integer(compute="_compute_fsa_counts", store=True)
    pickup_fsa_count = fields.Integer(compute="_compute_fsa_counts", store=True)
    delivery_fsa_count = fields.Integer(compute="_compute_fsa_counts", store=True)

    @api.depends("fsa_ids.active", "fsa_ids.pickup_supported", "fsa_ids.delivery_supported")
    def _compute_fsa_counts(self):
        for rec in self:
            rec.active_fsa_count = len(rec.fsa_ids.filtered("active"))
            rec.pickup_fsa_count = len(rec.fsa_ids.filtered(lambda f: f.active and f.pickup_supported))
            rec.delivery_fsa_count = len(rec.fsa_ids.filtered(lambda f: f.active and f.delivery_supported))
    map_color = fields.Char(string="Map Color")

    _sql_constraints = [
        ("code_uniq", "unique(code)", "Region code must be unique."),
    ]

    @api.model
    def get_network_map_data(self):
        """Where We Go — static reference data (regions + hubs) for the map's
        pickup selector. Requires Dispatcher or Logistics Manager group.
        Destinations for a chosen pickup are fetched separately via
        get_network_destinations(), not eagerly computed here."""
        _require_dispatch_staff(self.env)
        Hub = self.env["logistics.hub"].sudo()

        regions = [{
            "id": r.id, "code": r.code, "name": r.name,
            "display_number": r.display_number or r.id,
            "main_city": r.main_city or "",
            "lat": r.marker_latitude or 44.0,
            "lng": r.marker_longitude or -78.0,
        } for r in self.sudo().search([("active", "=", True), ("customer_visible", "=", True)])]

        hubs = [{
            "id": h.id, "name": h.name, "public_name": h.public_name,
            "lat": h.latitude or 43.649, "lng": h.longitude or -79.659,
            "is_default": h.is_default,
        } for h in Hub.search([("active", "=", True)])]

        api_key = self.env["ir.config_parameter"].sudo().get_param("google_maps_api_key", "")
        return {"regions": regions, "hubs": hubs, "google_api_key": api_key or ""}

    @api.model
    def get_network_destinations(self, origin_model, origin_id, equipment="dry"):
        """Where We Go — destinations reachable from one pickup (a region or
        the hub itself). Requires Dispatcher or Logistics Manager group."""
        _require_dispatch_staff(self.env)
        if origin_model not in ("logistics.region", "logistics.hub"):
            return []
        origin = self.env[origin_model].sudo().browse(int(origin_id))
        if not origin.exists():
            return []

        from ..services.network_availability_service import NetworkAvailabilityService
        return NetworkAvailabilityService(self.env).list_destinations_from(origin, equipment=equipment)

    def name_get(self):
        return [(r.id, f"{r.code} - {r.name}") for r in self]

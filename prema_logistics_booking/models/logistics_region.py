import re

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


FSA_RE = re.compile(r"^[A-Z][0-9][A-Z]$")


def _require_dispatch_staff(env):
    if not env.user.has_group("prema_dispatch.group_dispatcher") and \
       not env.user.has_group("prema_dispatch.group_dispatch_manager"):
        raise AccessError(_("Only dispatchers and logistics managers can view the network map."))


class LogisticsRegion(models.Model):
    _name = "logistics.region"
    _description = "Service Region — routing, postal coverage, and map authority"
    _order = "display_sequence, code"

    code = fields.Char(required=True, index=True)
    name = fields.Char(required=True)
    display_number = fields.Integer(string="Display Number", help="Number on map marker")
    main_city = fields.Char(string="Main City")
    hub_name = fields.Char(string="Hub")

    rate_per_km = fields.Float(
        string="Rate per km [DEPRECATED]", default=3.00,
        help="Historical only. Customer pricing is configured on Corridors.",
    )

    # ── Map fields ────────────────────────────────────────────────────
    marker_latitude = fields.Float(string="Marker Latitude", digits=(10, 6))
    marker_longitude = fields.Float(string="Marker Longitude", digits=(10, 6))
    map_anchor_address = fields.Char(
        string="Region Map Anchor",
        help="Choose the region capital or representative city through Google Places.",
    )
    map_anchor_place_id = fields.Char(string="Map Anchor Google Place ID", copy=False)
    polygon_geojson = fields.Text(string="Polygon GeoJSON")
    public_description = fields.Text(string="Public Description")
    default_hub_id = fields.Many2one("logistics.hub", string="Default Hub")

    # ── Status ────────────────────────────────────────────────────────
    customer_visible = fields.Boolean(default=True, string="Customer Visible")
    phase = fields.Integer(default=1)
    display_sequence = fields.Integer(default=10)
    is_official_ltl_region = fields.Boolean(
        string="Official LTL Region", default=False, index=True,
        help="Only approved Prema LTL regions are offered in new route and recurring-job setup.",
    )
    active = fields.Boolean(default=True)

    # ── Postal Coverage ──────────────────────────────────────────────
    fsa_ids = fields.One2many("logistics.fsa", "region_id", string="FSAs")
    coverage_fsa_prefixes = fields.Text(
        string="Approved FSA Prefixes",
        help="Paste comma-, space-, or line-separated three-character Canadian FSAs, then apply them. "
             "Conflicts with another Region are blocked instead of silently reassigned.",
    )
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

    @staticmethod
    def _province_for_fsa(fsa):
        first = fsa[:1]
        return {
            "A": "NL", "B": "NS", "C": "PE", "E": "NB",
            "G": "QC", "H": "QC", "J": "QC",
            "K": "ON", "L": "ON", "M": "ON", "N": "ON", "P": "ON",
            "R": "MB", "S": "SK", "T": "AB", "V": "BC", "Y": "YT",
        }.get(first, False)

    def action_apply_fsa_coverage(self):
        """Apply a reviewed FSA list without guessing regional boundaries."""
        if not self.env.user.has_group("prema_dispatch.group_dispatch_manager"):
            raise AccessError(_("Only Dispatch Managers can change Region postal coverage."))
        Fsa = self.env["logistics.fsa"].sudo()
        for region in self:
            prefixes = sorted(set(filter(None, re.split(
                r"[\s,;]+", (region.coverage_fsa_prefixes or "").upper().strip(),
            ))))
            if not prefixes:
                raise UserError(_("Enter at least one approved three-character FSA."))
            invalid = [prefix for prefix in prefixes if not FSA_RE.match(prefix)]
            if invalid:
                raise UserError(_(
                    "Invalid FSA prefix(es): %s. Use three characters such as L5M, K1G, or H3B."
                ) % ", ".join(invalid))

            existing = Fsa.search([("fsa", "in", prefixes)])
            conflicts = existing.filtered(
                lambda fsa: fsa.region_id and fsa.region_id != region
            )
            if conflicts:
                details = ", ".join(
                    f"{fsa.fsa} ({fsa.region_id.name})" for fsa in conflicts
                )
                raise UserError(_(
                    "These FSAs already belong to another Region: %s. "
                    "Review the boundary instead of overwriting it."
                ) % details)

            existing.write({"region_id": region.id, "active": True})
            existing_codes = set(existing.mapped("fsa"))
            for prefix in prefixes:
                if prefix in existing_codes:
                    continue
                Fsa.create({
                    "fsa": prefix,
                    "province": self._province_for_fsa(prefix),
                    "display_city": region.main_city or region.name,
                    "region_id": region.id,
                    "pickup_supported": True,
                    "delivery_supported": True,
                    "active": True,
                })
            region.coverage_fsa_prefixes = "\n".join(prefixes)

        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {
                "title": _("FSA coverage applied"),
                "message": _("Postal coverage was validated and assigned without conflicts."),
                "type": "success", "sticky": False,
            },
        }

    @api.model
    def get_network_map_data(self):
        """Where We Go — static reference data (regions + hubs) for the map's
        pickup selector. Requires Dispatcher or Logistics Manager group.
        Destinations for a chosen pickup are fetched separately via
        get_network_destinations(), not eagerly computed here."""
        _require_dispatch_staff(self.env)
        Hub = self.env["logistics.hub"].sudo()

        pickup_regions = self.env["logistics.corridor.stop"].sudo().search([
            ("active", "=", True),
            ("pickup_allowed", "=", True),
            ("corridor_id.active", "=", True),
            ("region_id", "!=", False),
        ]).mapped("region_id")
        regions = [{
            "id": r.id, "code": r.code, "name": r.name,
            "display_number": r.display_number or r.id,
            "main_city": r.main_city or "",
            "lat": r.marker_latitude or False,
            "lng": r.marker_longitude or False,
        } for r in pickup_regions.filtered(
            lambda region: region.active and region.customer_visible
        ).sorted(key=lambda region: (region.display_sequence, region.name or ""))]

        hubs = [{
            "id": h.id, "name": h.name, "public_name": h.public_name,
            "lat": h.latitude or h.saved_location_id.pin_lat or False,
            "lng": h.longitude or h.saved_location_id.pin_lng or False,
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

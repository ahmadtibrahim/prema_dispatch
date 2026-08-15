import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)


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
        string="Rate per km", default=3.00,
        help="Historical only. Customer pricing is configured on Corridors.",
    )

    # ── Pricing overrides ────────────────────────────────────────────
    currency_id = fields.Many2one(
        "res.currency", default=lambda self: self.env.company.currency_id,
    )
    minimum_booking_charge = fields.Monetary(
        string="Minimum Booking Charge",
        currency_field="currency_id",
        default=0.0,
        help=(
            "Optional pricing floor for any booking where this Region is the "
            "pickup or delivery endpoint. If set, it overrides corridor-level "
            "minimums by taking the maximum."
        ),
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

    # ── Boundary metadata ──────────────────────────────────────────────
    boundary_source = fields.Char(
        string="Boundary Source",
        help="e.g. 'Statistics Canada 2021 Census Boundary File', "
             "'Custom aggregation by Dispatch Manager'",
    )
    boundary_source_url = fields.Char(string="Boundary Source URL")
    boundary_version_date = fields.Date(string="Boundary Version Date")
    boundary_status = fields.Selection([
        ("draft", "Draft"),
        ("proposed", "Proposed"),
        ("reviewed", "Reviewed"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    ], default="draft", string="Boundary Status",
       help="Only 'Approved' boundaries participate in live region matching.")
    boundary_reviewed_by = fields.Many2one(
        "res.users", string="Boundary Reviewed By", readonly=True, copy=False,
    )
    boundary_reviewed_at = fields.Datetime(
        string="Boundary Reviewed At", readonly=True, copy=False,
    )
    boundary_area_km2 = fields.Float(
        string="Boundary Area (km²)", digits=(12, 3), readonly=True,
        help="Computed from the polygon geometry.",
    )
    boundary_checksum = fields.Char(
        string="Boundary Checksum", readonly=True, copy=False,
        help="SHA-256 of the normalized polygon. Used for cache invalidation.",
    )
    match_priority = fields.Integer(
        string="Match Priority", default=10,
        help="Higher priority wins when two approved regions overlap. "
             "Default is 10; increase for smaller/more specific regions.",
    )

    # ── Country / Province ────────────────────────────────────────────
    country_id = fields.Many2one(
        "res.country", string="Country",
        default=lambda self: self.env.ref("base.ca"),
        index=True,
        help="Country this region belongs to. Determines available provinces/states.",
    )
    state_id = fields.Many2one(
        "res.country.state", string="Province / State",
        domain="[('country_id', '=', country_id)]",
        index=True,
        help="Province or state this region belongs to.",
    )

    # ── Status ────────────────────────────────────────────────────────
    customer_visible = fields.Boolean(default=True, string="Customer Visible")
    phase = fields.Integer(default=1)
    display_sequence = fields.Integer(default=10)
    is_official_ltl_region = fields.Boolean(
        string="Official LTL Region", default=False, index=True,
        help="Only approved Prema LTL regions are offered in new route and recurring-job setup.",
    )
    active = fields.Boolean(default=True)

    def _is_network_available(self):
        """Check the full hierarchy for new logistics operations:

        region.active = True
        AND region.country_id exists
        AND region.country_id.logistics_network_enabled = True
        AND region.state_id exists
        AND region.state_id.logistics_network_enabled = True

        Returns False if any link in the chain is unavailable.
        Historical records are unaffected — this is for NEW operations only."""
        self.ensure_one()
        if not self.active:
            return False
        if not self.country_id:
            return False
        if not self.country_id.logistics_network_enabled:
            return False
        if not self.state_id:
            return False
        if not self.state_id.logistics_network_enabled:
            return False
        return True

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

    @api.model
    def get_corridor_topology(self, hub_id=None):
        """Return complete corridor topology for Where We Go network map.
        Hub → ordered regions → Hub. Includes region polygons."""
        _require_dispatch_staff(self.env)
        import json

        Corridor = self.env["logistics.corridor"].sudo()
        Hub = self.env["logistics.hub"].sudo()
        Region = self.env["logistics.region"].sudo()

        if hub_id:
            hub = Hub.browse(int(hub_id)).exists()
        if not (hub_id and hub):
            hub = Hub.search([("is_default", "=", True), ("active", "=", True)], limit=1)
        if not hub:
            return {"error": "No hub found"}

        hub_payload = {
            "id": hub.id,
            "name": hub.public_name or hub.name,
            "lat": hub.latitude or (hub.saved_location_id.pin_lat if hub.saved_location_id else 0),
            "lng": hub.longitude or (hub.saved_location_id.pin_lng if hub.saved_location_id else 0),
        }

        all_stops = self.env["logistics.corridor.stop"].sudo().search([
            ("active", "=", True),
        ], order="corridor_id, sequence")
        corridor_ids = all_stops.mapped("corridor_id").ids
        corridors = Corridor.search([("id", "in", corridor_ids), ("active", "=", True)])

        DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        corridor_list = []
        all_region_ids = set()

        for c in corridors:
            c_stops = all_stops.filtered(lambda s, cid=c.id: s.corridor_id.id == cid)
            regions = []
            for s in c_stops:
                r = s.region_id
                all_region_ids.add(r.id)
                regions.append({
                    "region_id": r.id, "code": r.code, "name": r.name,
                    "lat": r.marker_latitude, "lng": r.marker_longitude,
                    "pickup_allowed": s.pickup_allowed,
                    "delivery_allowed": s.delivery_allowed,
                })

            operating_days = [d[:3] for d in DAYS if getattr(c, f"operate_{d}")]

            corridor_list.append({
                "id": c.id, "name": c.name,
                "operating_days": operating_days,
                "regions": regions,
            })

        all_regions = Region.search([("id", "in", list(all_region_ids))])
        region_payloads = []
        for r in all_regions:
            entry = {
                "id": r.id, "code": r.code, "name": r.name,
                "lat": r.marker_latitude, "lng": r.marker_longitude,
                "main_city": r.main_city or "",
            }
            if r.polygon_geojson:
                try:
                    entry["geojson"] = json.loads(r.polygon_geojson)
                except Exception:
                    pass
            region_payloads.append(entry)

        manual_regions = Region.search([
            ("active", "=", True), ("customer_visible", "=", True),
            ("id", "not in", list(all_region_ids)),
        ])
        for r in manual_regions:
            entry = {
                "id": r.id, "code": r.code, "name": r.name,
                "lat": r.marker_latitude, "lng": r.marker_longitude,
                "main_city": r.main_city or "", "manual_quote": True,
            }
            if r.polygon_geojson:
                try:
                    entry["geojson"] = json.loads(r.polygon_geojson)
                except Exception:
                    pass
            region_payloads.append(entry)

        hubs = Hub.search([("active", "=", True)])
        hub_list = [{"id": h.id, "name": h.public_name or h.name,
                      "is_default": h.is_default} for h in hubs]

        return {
            "hub": hub_payload,
            "corridors": corridor_list,
            "regions": region_payloads,
            "hubs": hub_list,
        }

    # ── Constraints ──────────────────────────────────────────────────

    @api.constrains("country_id", "state_id")
    def _check_state_belongs_to_country(self):
        """Ensure the selected province/state belongs to the selected country."""
        for region in self:
            if region.country_id and region.state_id:
                if region.state_id.country_id != region.country_id:
                    raise UserError(_(
                        "The province/state '%(state)s' does not belong to "
                        "the country '%(country)s'. Please select a "
                        "province/state within the chosen country.",
                        state=region.state_id.name,
                        country=region.country_id.name,
                    ))

    # ── Cascading deactivation ────────────────────────────────────────

    @api.model
    def _get_available_regions_domain(self):
        """Domain for regions available for new operations.

        Checks the full hierarchy:
          region.active = True
          AND region.is_official_ltl_region = True
          AND country_id.logistics_network_enabled = True
          AND state_id.logistics_network_enabled = True
        """
        return [
            ("active", "=", True),
            ("is_official_ltl_region", "=", True),
            ("country_id", "!=", False),
            ("country_id.logistics_network_enabled", "=", True),
            ("state_id", "!=", False),
            ("state_id.logistics_network_enabled", "=", True),
        ]

    def write(self, vals):
        """Detect deactivation and log. If active is being set to False,
        log the change for audit. Cascading effects (corridor stops, future
        departures, bookings) are handled by downstream processes that
        filter by _get_available_regions_domain()."""
        if "active" in vals and not vals["active"]:
            for region in self:
                _logger.info(
                    "Region %s (ID %s) deactivated. Country=%s State=%s. "
                    "Historical records preserved. Region unavailable for new operations.",
                    region.code, region.id,
                    region.country_id.name if region.country_id else "N/A",
                    region.state_id.name if region.state_id else "N/A",
                )
        return super().write(vals)

    # ── Boundary management ─────────────────────────────────────────

    def action_validate_boundary(self):
        """Validate the polygon_geojson field using Shapely.

        Computes boundary_area_km2 and boundary_checksum if valid.
        Updates boundary_status to 'reviewed' if currently 'draft' or 'proposed'.
        """
        from ..services.region_resolver import RegionResolver

        resolver = RegionResolver(self.env)
        for region in self:
            if not region.polygon_geojson:
                raise UserError(_("Region %s has no polygon GeoJSON to validate.") % region.code)

            is_valid, message, repaired = resolver.validate_geometry(region.polygon_geojson)

            vals = {}
            if is_valid:
                vals["boundary_area_km2"] = resolver.compute_area_km2(region.polygon_geojson)
                vals["boundary_checksum"] = resolver.compute_checksum(region.polygon_geojson)
                if region.boundary_status in ("draft", "proposed"):
                    vals["boundary_status"] = "reviewed"
                    vals["boundary_reviewed_by"] = self.env.user.id
                    vals["boundary_reviewed_at"] = fields.Datetime.now()
                region.write(vals)
                # Clear geometry cache
                resolver.invalidate_cache(region)
            else:
                if repaired:
                    raise UserError(_(
                        "Boundary validation failed for %(code)s:\n\n%(msg)s\n\n"
                        "A repaired geometry is available. Review the proposed "
                        "repair before applying it.",
                        code=region.code, msg=message,
                    ))
                else:
                    raise UserError(_(
                        "Boundary validation failed for %(code)s:\n\n%(msg)s",
                        code=region.code, msg=message,
                    ))

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Boundary Validated"),
                "message": _("Polygon is valid. Area and checksum updated."),
                "type": "success",
                "sticky": False,
            },
        }

    def action_preview_boundary(self):
        """Return an action to preview the boundary on a map.
        For now, returns the region form with the map anchor focused."""
        return {
            "type": "ir.actions.act_window",
            "res_model": "logistics.region",
            "res_id": self.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_check_overlaps(self):
        """Check for polygon overlaps with other regions in the same province."""
        from ..services.region_resolver import RegionResolver

        resolver = RegionResolver(self.env)
        overlaps = resolver.detect_overlaps(self)

        if not overlaps:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("No Overlaps"),
                    "message": _("This region does not overlap with any other "
                                 "approved region in the same province."),
                    "type": "success",
                    "sticky": False,
                },
            }

        # Build a message summarizing overlaps
        lines = []
        for o in overlaps:
            lines.append(_(
                "%(code)s (%(name)s): %(area).1f km² overlap "
                "(%(pct).1f%% of the smaller region) — %(severity)s",
                code=o["region_b"].code,
                name=o["region_b"].name,
                area=o["overlap_area_km2"],
                pct=max(o["overlap_pct_a"], o["overlap_pct_b"]),
                severity=o["severity"],
            ))

        raise UserError(_(
            "Overlap detected with %(count)d region(s):\n\n%(lines)s\n\n"
            "Review and adjust boundaries or set match_priority values.",
            count=len(overlaps),
            lines="\n".join(lines),
        ))

    def action_test_coordinate(self):
        """Test a coordinate against this region's polygon.

        Opens a simple prompt to enter lat/lng and shows the result.
        """
        return {
            "type": "ir.actions.act_window",
            "name": _("Test Coordinate — %s") % self.code,
            "res_model": "logistics.region.test.coordinate",
            "view_mode": "form",
            "target": "new",
            "context": {"default_region_id": self.id},
        }

    def name_get(self):
        return [(r.id, f"{r.code} - {r.name}") for r in self]

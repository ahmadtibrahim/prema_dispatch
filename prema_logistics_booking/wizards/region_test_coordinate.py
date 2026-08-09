"""Transient wizard for testing a coordinate against region polygons."""

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class LogisticsRegionTestCoordinate(models.TransientModel):
    _name = "logistics.region.test.coordinate"
    _description = "Test Coordinate Against Region Polygon"

    region_id = fields.Many2one(
        "logistics.region", string="Region", required=True, readonly=True,
    )
    latitude = fields.Float(string="Latitude", required=True, digits=(10, 6))
    longitude = fields.Float(string="Longitude", required=True, digits=(10, 6))
    result_matched = fields.Boolean(string="Matched", readonly=True)
    result_region_code = fields.Char(string="Matched Region", readonly=True)
    result_region_name = fields.Char(string="Region Name", readonly=True)
    result_match_method = fields.Char(string="Matching Method", readonly=True)
    result_reason = fields.Char(string="Reason", readonly=True)
    result_ambiguity = fields.Boolean(string="Ambiguity", readonly=True)
    result_candidates = fields.Text(string="Candidate Regions", readonly=True)

    def action_test(self):
        """Run the coordinate against the selected region's polygon."""
        self.ensure_one()
        from ..services.region_resolver import RegionResolver

        resolver = RegionResolver(self.env)
        result = resolver.resolve(
            latitude=self.latitude,
            longitude=self.longitude,
            country=self.region_id.country_id.id,
            state=self.region_id.state_id.id,
        )

        self.write({
            "result_matched": result.matched_region is not None,
            "result_region_code": result.matched_region_code or "",
            "result_region_name": result.matched_region.name if result.matched_region else "",
            "result_match_method": result.match_method,
            "result_reason": result.reason,
            "result_ambiguity": result.ambiguity,
            "result_candidates": ", ".join(
                f"{r.code} ({r.name})" for r in result.candidate_regions
            ) if result.candidate_regions else "",
        })

        # Show result in a notification
        if result.matched_region:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Match Found: %s") % result.matched_region_code,
                    "message": result.reason,
                    "type": "success",
                    "sticky": True,
                },
            }
        else:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("No Match"),
                    "message": result.reason,
                    "type": "warning",
                    "sticky": True,
                },
            }

    def action_test_all_regions(self):
        """Test the coordinate against ALL active approved regions."""
        self.ensure_one()
        from ..services.region_resolver import RegionResolver

        resolver = RegionResolver(self.env)
        result = resolver.resolve(
            latitude=self.latitude,
            longitude=self.longitude,
        )

        vals = {
            "result_matched": result.matched_region is not None,
            "result_region_code": result.matched_region_code or "",
            "result_region_name": result.matched_region.name if result.matched_region else "",
            "result_match_method": result.match_method,
            "result_reason": result.reason,
            "result_ambiguity": result.ambiguity,
            "result_candidates": ", ".join(
                f"{r.code} ({r.name})" for r in result.candidate_regions
            ) if result.candidate_regions else "",
        }
        self.write(vals)

        if result.matched_region:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Match Found: %s") % result.matched_region_code,
                    "message": result.reason,
                    "type": "success" if not result.ambiguity else "warning",
                    "sticky": True,
                },
            }
        else:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("No Match"),
                    "message": result.reason,
                    "type": "warning",
                    "sticky": True,
                },
            }

"""Direct Delivery Matrix — determines whether a region pair can bypass the Hub."""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

DAY_SELECTION = [
    ("monday", "Monday"),
    ("tuesday", "Tuesday"),
    ("wednesday", "Wednesday"),
    ("thursday", "Thursday"),
    ("friday", "Friday"),
    ("saturday", "Saturday"),
]

DIRECTION_SELECTION = [
    ("outbound", "Outbound (Hub → Region)"),
    ("inbound", "Inbound (Region → Hub)"),
    ("both", "Both Directions"),
]


class LogisticsDirectDeliveryRule(models.Model):
    _name = "logistics.direct.delivery.rule"
    _description = "Direct Delivery Rule"
    _order = "origin_region_id, destination_region_id"

    name = fields.Char(string="Rule Name", compute="_compute_name", store=True)
    active = fields.Boolean(default=True)

    # ── Geography ────────────────────────────────────────────────────
    origin_region_id = fields.Many2one(
        "logistics.region", string="Origin Region", required=True, index=True,
        domain="[('is_official_ltl_region', '=', True), ('active', '=', True)]",
    )
    destination_region_id = fields.Many2one(
        "logistics.region", string="Destination Region", required=True, index=True,
        domain="[('is_official_ltl_region', '=', True), ('active', '=', True)]",
    )

    # ── Corridor ─────────────────────────────────────────────────────
    applicable_corridor_id = fields.Many2one(
        "logistics.corridor", string="Applicable Corridor",
        help="The corridor this direct rule operates on.",
    )

    # ── Schedule ─────────────────────────────────────────────────────
    allowed_service_days = fields.Char(
        string="Allowed Service Days",
        help="Comma-separated lowercase days: monday,tuesday,…",
    )
    # Parsed as list for service use
    allowed_days_list = fields.Char(compute="_compute_allowed_days_list", store=False)

    direction = fields.Selection(
        DIRECTION_SELECTION, string="Direction",
        required=True, default="both",
        help="Direction of travel this rule applies to.",
    )

    # ── Routing Decision ─────────────────────────────────────────────
    direct_same_day_allowed = fields.Boolean(
        string="Direct Same-Day Allowed", default=False,
        help="Freight may move directly from origin to destination region "
             "on the same day without Hub transfer.",
    )
    hub_transfer_required = fields.Boolean(
        string="Hub Transfer Required", default=True,
        help="Freight must route through the Hub.",
    )

    # ── Operational Limits ───────────────────────────────────────────
    max_direct_distance_km = fields.Float(
        string="Max Direct Distance (km)",
        help="Maximum Google road distance for direct delivery. "
             "Leave 0 for no distance restriction.",
    )
    earliest_pickup_time = fields.Float(
        string="Earliest Pickup (hrs)",
        help="Earliest pickup time in hours, e.g. 6.0 = 6:00 AM.",
    )
    latest_pickup_time = fields.Float(
        string="Latest Pickup (hrs)",
        help="Latest pickup time in hours for same-day direct delivery.",
    )
    latest_delivery_time = fields.Float(
        string="Latest Delivery (hrs)",
        help="Latest delivery time in hours for same-day direct delivery.",
    )
    operational_notes = fields.Text(string="Operational Notes")

    # ── Constraints ──────────────────────────────────────────────────
    @api.constrains("direct_same_day_allowed", "hub_transfer_required")
    def _check_routing_decision_consistent(self):
        for rule in self:
            if rule.direct_same_day_allowed and rule.hub_transfer_required:
                raise ValidationError(_(
                    "A rule cannot be both 'Direct Same-Day Allowed' and "
                    "'Hub Transfer Required'. Choose one routing decision."
                ))
            if not rule.direct_same_day_allowed and not rule.hub_transfer_required:
                raise ValidationError(_(
                    "A rule must specify either 'Direct Same-Day Allowed' "
                    "or 'Hub Transfer Required'."
                ))

    @api.constrains("origin_region_id", "destination_region_id")
    def _check_different_regions(self):
        for rule in self:
            if rule.origin_region_id == rule.destination_region_id:
                raise ValidationError(_(
                    "Origin and destination regions must be different. "
                    "Intra-region movement is handled automatically."
                ))

    @api.constrains("origin_region_id", "destination_region_id", "direction", "active")
    def _check_unique_active_rule(self):
        """Only one ACTIVE rule may exist per origin+destination+direction.
        Archived/inactive historical versions are allowed."""
        for rule in self:
            if not rule.active:
                continue
            conflicting = self.search([
                ("id", "!=", rule.id),
                ("origin_region_id", "=", rule.origin_region_id.id),
                ("destination_region_id", "=", rule.destination_region_id.id),
                ("direction", "=", rule.direction),
                ("active", "=", True),
            ])
            if conflicting:
                raise ValidationError(_(
                    "An active rule already exists for %(origin)s → %(dest)s "
                    "(%(direction)s). Archive the existing rule before creating "
                    "a new active one.",
                    origin=rule.origin_region_id.code,
                    dest=rule.destination_region_id.code,
                    direction=rule.direction,
                ))

    # ── Computed ─────────────────────────────────────────────────────
    @api.depends("origin_region_id", "destination_region_id", "direction")
    def _compute_name(self):
        for rule in self:
            origin = rule.origin_region_id.code or "?"
            dest = rule.destination_region_id.code or "?"
            direction = dict(DIRECTION_SELECTION).get(rule.direction, rule.direction)
            rule.name = f"{origin} → {dest} ({direction})"

    def _compute_allowed_days_list(self):
        for rule in self:
            rule.allowed_days_list = rule.allowed_service_days or ""

    def get_allowed_days(self):
        """Return list of allowed day strings for this rule."""
        if not self.allowed_service_days:
            return []
        return [d.strip().lower() for d in self.allowed_service_days.split(",") if d.strip()]

    def is_day_allowed(self, day_name):
        """Check if a given day (lowercase) is allowed by this rule."""
        return day_name.lower() in self.get_allowed_days()

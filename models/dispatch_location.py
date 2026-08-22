import hashlib
import logging
import re

from odoo import api, fields, models
from odoo.exceptions import ValidationError
from odoo.osv import expression

_logger = logging.getLogger(__name__)


class PremaDispatchLocation(models.Model):
    """Saved delivery/pickup locations — remembers exact parking pin and entrance photo.

    When a stop address is matched against a saved location, the precise
    pin position (pin_lat/pin_lng) is pre-loaded so drivers always park in
    the right spot without re-pinning every time.
    """
    _name = "prema.dispatch.location"
    _inherit = ["mail.thread"]
    _description = "Saved Dispatch Location"
    _order = "use_count desc, name"
    _rec_name = "name"

    name = fields.Char(string="Location Name", required=True)
    business_name = fields.Char(
        string="Business Name",
        help="The customer/warehouse's actual business name — shown on the driver app "
             "so repeat stops (e.g. two pickups from the same shipper) are recognizable "
             "at a glance instead of just an address.",
    )
    branch_name = fields.Char(
        string="Branch Name",
        help="Franchise operator or branch identifier — e.g. 'Belleville', 'Peterborough', "
             "'Milton'. Kept separate from the business name so two branches of the same "
             "chain are distinguishable.",
    )
    address = fields.Char(string="Address", required=True)

    chain_name = fields.Char(string="Chain / Brand", index=True)
    location_number = fields.Char(string="Store / Location #", index=True)
    location_number_normalized = fields.Char(compute="_compute_location_search_fields", store=True, index=True)

    # ── Normalized fields (computed, stored, indexed for efficient duplicate scans) ──
    normalized_brand = fields.Char(compute="_compute_location_search_fields", store=True, index=True)
    normalized_business = fields.Char(compute="_compute_location_search_fields", store=True, index=True)
    normalized_branch = fields.Char(compute="_compute_location_search_fields", store=True, index=True)
    normalized_unit = fields.Char(compute="_compute_location_search_fields", store=True, index=True)

    location_search_key = fields.Char(compute="_compute_location_search_fields", store=True, index=True)
    location_display_label = fields.Char(
        compute="_compute_location_search_fields",
        search="_search_location_anywhere",
        help="Display label (Business — City), and the Saved Locations search "
             "box entry point: its search method routes every query through "
             "normalized word-AND matching over location_search_key. This field "
             "(rather than a brand-new one) must remain the search-view target "
             "so clients with pre-upgrade cached field payloads never see an "
             "unknown field.",
    )
    street = fields.Char()
    street2 = fields.Char()
    unit = fields.Char()
    city = fields.Char(index=True)
    province_code = fields.Char(string="Province", size=8)
    postal_code = fields.Char(index=True)
    country_id = fields.Many2one("res.country", string="Country")
    normalized_address = fields.Char(compute="_compute_location_search_fields", store=True, index=True)
    normalized_address_hash = fields.Char(compute="_compute_location_search_fields", store=True, index=True)
    verification_state = fields.Selection([
        ("driver_submitted", "Driver Submitted"), ("pending_review", "Pending Review"),
        ("verified", "Verified"), ("rejected", "Rejected"), ("legacy", "Legacy / Imported"),
    ], default="driver_submitted", index=True)
    source_type = fields.Selection([
        ("dispatcher_manual", "Dispatcher Manual"), ("driver_manual", "Driver Manual"),
        ("photo_extraction", "Photo Extraction"), ("google_places", "Google Places"), ("imported", "Imported"),
    ])
    created_by_driver_id = fields.Many2one("res.partner")
    last_verified_by = fields.Many2one("res.users")
    last_verified_at = fields.Datetime()
    pin_source = fields.Selection([
        ("geocoded_address", "Geocoded Address"), ("google_place", "Google Place"),
        ("driver_gps", "Driver GPS"), ("driver_map", "Driver Map"),
        ("dispatcher_map", "Dispatcher Map"), ("imported", "Imported"),
    ])
    pin_accuracy_m = fields.Float()

    google_place_id = fields.Char(
        string="Google Place ID", index=True,
        help="Google Places identifier for precise address matching.",
    )
    google_verified = fields.Boolean(
        string="Google Verified",
        default=False,
        help="True when the address was selected from Google Places and has not been "
             "manually edited since. Cleared automatically when any address field is "
             "hand-edited.",
    )

    # Address Validation API (same pattern as prema.dispatch.stop)
    address_validated = fields.Boolean(string="Address Validated", readonly=True)
    address_validation_warning = fields.Char(string="Address Warning", readonly=True)
    address_formatted = fields.Char(string="Validated Address", readonly=True)
    partner_id = fields.Many2one(
        "res.partner", string="Customer / Warehouse",
        ondelete="set null",
        help="Link to a contact if this location belongs to a specific customer.",
    )

    # Precise parking pin (manually set by dispatcher or driver)
    pin_lat = fields.Float(string="Pin Latitude",  digits=(10, 6))
    pin_lng = fields.Float(string="Pin Longitude", digits=(10, 6))
    pin_set = fields.Boolean(
        string="Pin Manually Set",
        help="True when a dispatcher or driver has manually positioned the parking pin.",
    )

    active = fields.Boolean(default=True)
    portal_reusable = fields.Boolean(
        string="Portal Reusable", default=False,
        help="When enabled, this location appears in portal Saved Location "
             "autocomplete for all authenticated customers. Use for shared "
             "facilities (chain stores, public warehouses, cross-docks).",
    )

    location_type = fields.Selection([
        ("warehouse",  "Warehouse"),
        ("customer",   "Customer"),
        ("relay",      "Relay / Transfer Point"),
        ("rest_area",  "Rest Area"),
        ("fuel_station", "Fuel Station"),
        ("gym", "Gym"),
        ("hotel_motel", "Motels / Hotels"),
        ("other",      "Other"),
    ], string="Location Type", default="customer")

    stop_type = fields.Selection([
        ("pickup", "Pickup"),
        ("delivery", "Delivery"),
        ("both", "Pickup & Delivery"),
    ], string="Stop Type", default="delivery", index=True,
       help="How Prema Dispatch is allowed to use this location: pickup only, "
            "delivery only, or both. Editable by authorized staff. This controls "
            "which booking selectors this location appears in — NOT the computed "
            "historical usage.")

    allow_cross_dock = fields.Boolean(
        string="Allow Cross-Dock",
        help="This location can be used to temporarily transfer freight between loads "
             "(e.g. drop one job's skid here, pick up another job's freight, come back "
             "for it later) — a property of a location, not a separate Location Type. "
             "A Warehouse most commonly allows this, but any location type can.",
    )

    usage_type = fields.Selection([
        ("pickup", "Pickup"),
        ("delivery", "Delivery"),
        ("both", "Both"),
        ("unknown", "Unknown"),
    ], string="Historical Usage", compute="_compute_usage_type", store=True, index=True,
       help="Whether this location is used for pickups, deliveries, or both — "
            "computed from the stop history so dispatchers and drivers can see "
            "at a glance what kind of stop to expect here.")

    # ── Duplicate detection ──
    duplicate_status = fields.Selection([
        ("clean", "Clean"),
        ("possible", "Possible Duplicate"),
        ("confirmed_unique", "Confirmed Unique"),
    ], string="Duplicate Status", default="clean", index=True)
    duplicate_of_id = fields.Many2one(
        "prema.dispatch.location", string="Possible Duplicate Of",
        index=True, ondelete="set null",
    )

    # Entrance photo
    entrance_photo = fields.Binary(string="Entrance Photo", attachment=True)
    entrance_photo_fname = fields.Char(string="Photo Filename")

    # Additional site photos (same pattern as entrance_photo/entrance_photo_fname)
    dock_photo = fields.Binary(string="Dock Photo", attachment=True)
    dock_photo_fname = fields.Char(string="Dock Photo Filename")
    parking_photo = fields.Binary(string="Parking Photo", attachment=True)
    parking_photo_fname = fields.Char(string="Parking Photo Filename")
    gate_photo = fields.Binary(string="Gate Photo", attachment=True)
    gate_photo_fname = fields.Char(string="Gate Photo Filename")
    receiving_office_photo = fields.Binary(string="Receiving Office Photo", attachment=True)
    receiving_office_photo_fname = fields.Char(string="Receiving Office Photo Filename")
    scale_photo = fields.Binary(string="Scale Photo", attachment=True)
    scale_photo_fname = fields.Char(string="Scale Photo Filename")
    loading_area_photo = fields.Binary(string="Loading Area Photo", attachment=True)
    loading_area_photo_fname = fields.Char(string="Loading Area Photo Filename")

    # Instructions
    parking_notes = fields.Text(
        string="Parking / Entrance Notes",
        help="Instructions for drivers: dock door, entrance side, gate code, etc.",
    )
    driver_instructions = fields.Text(
        string="Driver Instructions",
        help="Structured turn-by-turn / arrival guidance for drivers, separate from the "
             "general dispatcher notes in Parking / Entrance Notes.",
    )
    security_notes = fields.Text(
        string="Security Notes",
        help="Security procedures, ID checks, sign-in requirements, etc.",
    )
    dock_door = fields.Char(string="Default Dock / Door #")
    receiving_entrance = fields.Char(string="Receiving Entrance")
    truck_entrance = fields.Char(string="Truck Entrance")
    gate_code = fields.Char(string="Gate Code")

    # Equipment / access
    liftgate_required = fields.Boolean(string="Liftgate Required")
    dock_height_available = fields.Boolean(string="Dock Height Available")
    pump_truck_required = fields.Boolean(string="Pump Truck Required")
    straight_truck_accessible = fields.Boolean(string="Straight Truck Accessible", default=True)
    trailer_53ft_accessible = fields.Boolean(string="53' Trailer Accessible", default=True)

    geofence_radius = fields.Integer(
        string="Geofence Radius (m)", default=150,
        help="Arrival geofence radius in meters — how close the driver's GPS must be "
             "before an arrival is auto-detected.",
    )

    # Stats
    use_count = fields.Integer(string="Times Visited", default=0, readonly=True)
    last_visited = fields.Datetime(string="Last Visited", readonly=True)

    # Auto-tracked visit stats (updated by record_visit_stats(), not user-entered)
    average_wait_minutes = fields.Float(string="Avg Wait (min)", readonly=True)
    average_unload_minutes = fields.Float(string="Avg Unload (min)", readonly=True)
    average_total_stop_minutes = fields.Float(string="Avg Total Stop (min)", readonly=True)
    fastest_stop_minutes = fields.Float(string="Fastest Stop (min)", readonly=True)
    longest_stop_minutes = fields.Float(string="Longest Stop (min)", readonly=True)
    average_delay_minutes = fields.Float(string="Avg Delay (min)", readonly=True)
    appointment_compliance_pct = fields.Float(string="Appointment Compliance %", readonly=True)
    arrival_accuracy_pct = fields.Float(string="Arrival Accuracy %", readonly=True)

    # ── Phase 6: sample-based timing statistics ("Historical stop timing and
    #    dwell estimates") — exact figures over the raw visit-sample table
    #    (visit_sample_ids), complementing the rolling running-averages above.
    visit_sample_ids = fields.One2many(
        "prema.dispatch.location.visit.sample", "location_id",
        string="Visit Samples", readonly=True,
    )
    median_dwell_minutes = fields.Float(string="Median Dwell (min)", readonly=True)
    avg_last10_dwell_minutes = fields.Float(
        string="Avg Dwell — Last 10 (min)", readonly=True)
    avg_loading_minutes = fields.Float(string="Avg Loading (min)", readonly=True)
    avg_unloading_minutes = fields.Float(string="Avg Unloading (min)", readonly=True)

    # AI/heuristic scores (readonly, computed — simple heuristics, not real ML)
    recommended_service_time_minutes = fields.Integer(string="Recommended Service Time (min)", readonly=True)
    best_arrival_window_start = fields.Char(string="Best Arrival Window Start", readonly=True)
    best_arrival_window_end = fields.Char(string="Best Arrival Window End", readonly=True)
    avoid_arrival_window_start = fields.Char(string="Avoid Arrival Window Start", readonly=True)
    avoid_arrival_window_end = fields.Char(string="Avoid Arrival Window End", readonly=True)
    difficulty_score = fields.Float(string="Difficulty Score", readonly=True)
    dock_efficiency_score = fields.Float(string="Dock Efficiency Score", readonly=True)
    traffic_impact_score = fields.Float(string="Traffic Impact Score", readonly=True)
    ai_confidence_score = fields.Float(
        string="AI Confidence Score", readonly=True,
        help="Reflects how many visits the stats above are based on — low with few "
             "visits, higher after many. Not a measure of accuracy, just sample size.",
    )

    # Linked stops (computed count)
    stop_ids = fields.One2many(
        "prema.dispatch.stop", "saved_location_id", string="Stops"
    )
    stop_count = fields.Integer(compute="_compute_stop_count", string="# Stops")

    # ═══════════════════════════════════════════════════════════════════════
    # Normalization Helpers
    # ═══════════════════════════════════════════════════════════════════════

    @api.model
    def _normalize_business(self, value):
        """Normalize a business / brand / branch name for duplicate comparison.

        - lowercase, trim, collapse whitespace
        - remove punctuation (including apostrophes)
        - & → and
        - strip trailing legal suffixes (Inc, Ltd, Corp, etc.)
        """
        if not value:
            return ""
        v = value.lower().strip()
        v = v.replace("&", " and ")
        v = re.sub(r"'", "", v)                               # Joe's → Joes
        v = re.sub(r"[.,|\":;!@#$%^*()_+=\[\]{}<>?/\\~`-]", " ", v)
        v = re.sub(r"\s+", " ", v).strip()
        v = re.sub(r"\s+(inc|ltd|limited|corp|corporation|company|co)\.?\s*$", "", v)
        return v.strip()

    @api.model
    def _normalize_address_street(self, value):
        """Normalize an address line: abbreviate street types, directions,
        remove punctuation, collapse whitespace.  Unit / suite is NOT part
        of the normalized address — it belongs in normalized_unit."""
        if not value:
            return ""
        v = value.lower().strip()
        v = re.sub(r"[,.]", "", v)
        v = re.sub(r"\s+", " ", v)
        street_map = {
            "street": "st", "road": "rd", "avenue": "ave", "boulevard": "blvd",
            "drive": "dr", "highway": "hwy", "lane": "ln", "court": "ct",
            "terrace": "terr", "circle": "cir", "parkway": "pkwy", "place": "pl",
            "square": "sq", "expressway": "expy", "freeway": "fwy",
            "north": "n", "south": "s", "east": "e", "west": "w",
            "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
        }
        # Whole-word replacement
        for full, abbr in street_map.items():
            v = re.sub(r'\b' + full + r'\b', abbr, v)
        return v.strip()

    @api.model
    def _normalize_unit(self, value):
        """Normalize a unit / suite number.

        Unit F / Suite F / #F / Apt F  →  f"""
        if not value:
            return ""
        v = value.lower().strip()
        v = re.sub(r'^(unit|suite|apt|apartment|room|ste|#)\s*', '', v, flags=re.I)
        v = re.sub(r'\s+', '', v)
        return v.strip()

    @api.model
    def _normalize_location_number(self, value):
        """Normalize a store / branch number.

        Store #678      → 678
        Store 03290     → 03290
        Branch DC-14    → DC14"""
        if not value:
            return ""
        v = value.strip()
        v = re.sub(r'^#', '', v)
        v = re.sub(r'^(STORE|LOCATION|BRANCH|WAREHOUSE)\s*#?\s*', '', v, flags=re.I)
        return re.sub(r'[\s-]+', '', v).upper()

    @api.model
    def _normalize_postal(self, value):
        """Normalize a postal / ZIP code: strip whitespace, uppercase."""
        if not value:
            return ""
        return re.sub(r'\s+', '', value).upper()

    @api.model
    def _normalize_text_key(self, value):
        """Collapse whitespace and uppercase — used for free-text search keys."""
        return re.sub(r"\s+", " ", (value or "").strip().upper())

    @api.model
    def _normalize_search_token(self, value):
        """Normalize one free-text search token (query word or key fragment).

        - lowercase → uppercase
        - & → and
        - apostrophes removed (Brandon's → BRANDONS)
        - punctuation and hyphens → space (no-frills → NO FRILLS)
        - whitespace collapsed

        The same normalizer runs on both the stored ``location_search_key``
        and the user's query, so the two always compare consistently.
        """
        if not value:
            return ""
        v = str(value).lower().strip()
        v = v.replace("&", " and ")
        v = re.sub(r"'", "", v)                                  # Joe's → Joes
        v = re.sub(r"[.,|\":;!@#$%^*()_+=\[\]{}<>?/\\~`-]", " ", v)
        return re.sub(r"\s+", " ", v).strip().upper()

    @api.model
    def _normalize_search_words(self, value, max_words=8):
        """Split a query into normalized individual search words.

        ``max_words`` caps the word-AND domain size so a pasted paragraph
        can't produce a pathological query.
        """
        return [w for w in self._normalize_search_token(value).split(" ") if w][:max_words]

    # ═══════════════════════════════════════════════════════════════════════
    # Computed Fields
    # ═══════════════════════════════════════════════════════════════════════

    @api.depends(
        "chain_name", "location_number", "business_name", "branch_name", "name",
        "city", "postal_code", "address", "street", "street2", "unit",
        "province_code", "country_id",
        "dock_door", "receiving_entrance", "truck_entrance", "gate_code",
        "partner_id.name",
    )
    def _compute_location_search_fields(self):
        for loc in self:
            # ── Simple normalizations ──
            loc.location_number_normalized = self._normalize_location_number(loc.location_number)
            loc.normalized_brand = self._normalize_business(loc.chain_name)
            loc.normalized_business = self._normalize_business(loc.business_name)
            loc.normalized_branch = self._normalize_business(loc.branch_name)
            loc.normalized_unit = self._normalize_unit(loc.unit)

            # ── Normalized address (without unit) ──
            country = loc.country_id.code or loc.country_id.name or ""
            raw_addr = " ".join(p for p in [
                loc.street, loc.street2, loc.city, loc.province_code,
                self._normalize_postal(loc.postal_code), country,
            ] if p)
            normalized = self._normalize_address_street(raw_addr or loc.address)
            loc.normalized_address = normalized
            loc.normalized_address_hash = (
                hashlib.sha256(normalized.encode()).hexdigest() if normalized else False
            )

            # ── Search key (free-text, all signals) ──
            # Everything the Saved Locations search box must find: display
            # name, chain/brand, business, branch, store #, unit, street/
            # address, city, postal code, customer and door/receiving info.
            key_parts = [
                loc.chain_name, loc.location_number_normalized,
                loc.business_name, loc.branch_name, loc.name,
                loc.city, loc.postal_code, loc.address, normalized,
                loc.unit, loc.normalized_unit,
                loc.dock_door, loc.receiving_entrance,
                loc.truck_entrance, loc.gate_code,
                loc.partner_id.name or "",
            ]
            loc.location_search_key = self._normalize_search_token(
                " ".join(p for p in key_parts if p)
            )

            # ── Display label ──
            label = ""
            if loc.chain_name:
                if loc.location_number_normalized:
                    label = "%s #%s" % (loc.chain_name, loc.location_number_normalized)
                else:
                    label = loc.chain_name
            elif loc.business_name:
                label = loc.business_name
            else:
                label = loc.name or ""

            if loc.branch_name:
                label = "%s — %s" % (label, loc.branch_name) if label else loc.branch_name
            elif loc.city and label:
                # Only append city when there is no branch (branch implies location)
                label = "%s — %s" % (label, loc.city)

            loc.location_display_label = label or loc.address or "Location"

    @api.model
    def _search_location_anywhere(self, operator, value):
        """Server-side 'search everywhere' for the Saved Locations search box.

        Splits the query into normalized words and requires every word to
        match somewhere in the stored ``location_search_key`` (word-AND), so
        multi-word queries like ``health niag`` match records whose words
        live in different fields. Case-, space-, punctuation-, apostrophe-
        and hyphen-insensitive — the query and the key go through the same
        ``_normalize_search_token``.

        This is the leaf the web client sends for the search-box facet:
        ``('location_display_label', 'ilike', '<typed text>')``.
        """
        if not isinstance(value, str):
            value = str(value)
        if operator.endswith("like"):
            words = self._normalize_search_words(value)
            if not words:
                # Optimize out the default criterion of ``like ''``.
                return (expression.FALSE_DOMAIN
                        if operator in expression.NEGATIVE_TERM_OPERATORS
                        else expression.TRUE_DOMAIN)
            return expression.AND([
                [("location_search_key", operator, word)] for word in words
            ])
        # Non-like operators ('=', '!='): direct comparison on the key.
        return [("location_search_key", operator, self._normalize_search_token(value))]

    # ═══════════════════════════════════════════════════════════════════════
    # Duplicate Detection
    # ═══════════════════════════════════════════════════════════════════════

    def _get_duplicate_candidates(self):
        """Return a recordset of potential duplicates, using indexed fields
        to narrow the search before detailed comparison."""
        self.ensure_one()
        if not self.normalized_address:
            return self.browse()

        domain_parts = []

        # Most precise: same Google Place ID
        if self.google_place_id:
            domain_parts.append([("google_place_id", "=", self.google_place_id)])

        # Same normalized address
        domain_parts.append([("normalized_address", "=", self.normalized_address)])

        # Same brand + store number (even different address)
        if self.normalized_brand and self.location_number_normalized:
            domain_parts.append([
                ("normalized_brand", "=", self.normalized_brand),
                ("location_number_normalized", "=", self.location_number_normalized),
            ])

        # Same street + postal code (even different/empty business — catches
        # portal-synced records whose brand/business fields are blank)
        if self.street and self.postal_code:
            domain_parts.append([
                ("street", "=ilike", self.street.strip()),
                ("postal_code", "=ilike", self.postal_code.strip()),
            ])

        domain = expression.OR(domain_parts)
        domain = expression.AND([domain, [("id", "!=", self.id), ("active", "=", True)]])

        return self.search(domain, limit=50)

    # ── Individual rule checks ──

    def _dup_rule1(self, other):
        """RULE 1: Same Google Place ID + Same Unit + Same Business  →  BLOCK"""
        return (
            self.google_place_id and other.google_place_id
            and self.google_place_id == other.google_place_id
            and self.normalized_unit == other.normalized_unit
            and self.normalized_business
            and self.normalized_business == other.normalized_business
        )

    def _dup_rule2(self, other):
        """RULE 2: Same Google Place ID + Same Unit + Same Brand + Same Store#  →  BLOCK"""
        return (
            self.google_place_id and other.google_place_id
            and self.google_place_id == other.google_place_id
            and self.normalized_unit == other.normalized_unit
            and self.normalized_brand
            and self.normalized_brand == other.normalized_brand
            and self.location_number_normalized
            and self.location_number_normalized == other.location_number_normalized
        )

    def _dup_rule3(self, other):
        """RULE 3: Same Address + Same Unit + Same Business  →  BLOCK"""
        return (
            self.normalized_address == other.normalized_address
            and self.normalized_unit == other.normalized_unit
            and self.normalized_business
            and self.normalized_business == other.normalized_business
        )

    def _dup_rule4(self, other):
        """RULE 4: Same Brand + Same Store Number  →  BLOCK  (even if address differs)"""
        return (
            self.normalized_brand
            and self.normalized_brand == other.normalized_brand
            and self.location_number_normalized
            and self.location_number_normalized == other.location_number_normalized
        )

    def _dup_rule6(self, other):
        """RULE 6: Same Address + Different Business  →  ALLOW but mark possible"""
        return (
            self.normalized_address == other.normalized_address
            and self.normalized_business
            and other.normalized_business
            and self.normalized_business != other.normalized_business
        )

    def _dup_rule10(self, other):
        """RULE 10: Same Google Place + Different Business  →  ALLOW but mark possible"""
        return (
            self.google_place_id and other.google_place_id
            and self.google_place_id == other.google_place_id
            and self.normalized_business
            and other.normalized_business
            and self.normalized_business != other.normalized_business
        )

    def _dup_rule11(self, other):
        """RULE 11: Same Street + Same Postal Code  →  ALLOW but mark possible.

        Catches records whose business/brand fields are empty or spelled
        differently (e.g. portal saved-location syncs) that the
        business-based rules miss. Records with two different units at the
        same address are genuinely different delivery points (two tenants in
        one plaza), so a unit mismatch opts out. Non-blocking: a dispatcher
        reviews and merges.
        """
        units_conflict = (
            self.normalized_unit and other.normalized_unit
            and self.normalized_unit != other.normalized_unit
        )
        return (
            self.street and other.street
            and self.postal_code and other.postal_code
            and not units_conflict
            and self._normalize_address_street(self.street) == self._normalize_address_street(other.street)
            and self._normalize_postal(self.postal_code) == self._normalize_postal(other.postal_code)
        )

    BLOCKING_RULES = ("_dup_rule1", "_dup_rule2", "_dup_rule3", "_dup_rule4")
    POSSIBLE_RULES = ("_dup_rule6", "_dup_rule10", "_dup_rule11")

    def _evaluate_duplicates(self):
        """Run all duplicate rules against candidates.

        :returns: dict with ``duplicate_status``, ``duplicate_of_id``, ``is_blocking``
        """
        self.ensure_one()
        result = {"duplicate_status": "clean", "duplicate_of_id": False, "is_blocking": False}

        candidates = self._get_duplicate_candidates()
        if not candidates:
            return result

        # Check blocking rules first
        for candidate in candidates:
            for rule_name in self.BLOCKING_RULES:
                if getattr(self, rule_name)(candidate):
                    result["duplicate_status"] = "possible"
                    result["duplicate_of_id"] = candidate.id
                    result["is_blocking"] = True
                    return result

        # Check non-blocking possible-duplicate rules
        for candidate in candidates:
            for rule_name in self.POSSIBLE_RULES:
                if getattr(self, rule_name)(candidate):
                    # RULE 11 pairs portal-synced copies against the canonical
                    # master facility. When this record has more real visit
                    # history than the candidate, THIS one is the primary —
                    # leave it clean and let the weaker side point here.
                    if rule_name == "_dup_rule11" and self.use_count > candidate.use_count:
                        continue
                    result["duplicate_status"] = "possible"
                    result["duplicate_of_id"] = candidate.id
                    return result

        # RULE 7 (Same Brand, Different Store# → ALLOW) — silent pass
        # RULE 8 (Same Business, Different Cities → ALLOW) — silent pass
        # RULE 9 (Same Google Place, Different Units → ALLOW) — silent pass
        return result

    # ═══════════════════════════════════════════════════════════════════════
    # Validation
    # ═══════════════════════════════════════════════════════════════════════

    @api.constrains(
        "google_place_id", "chain_name", "location_number", "business_name",
        "branch_name", "unit", "address", "street", "city", "postal_code",
        "country_id", "active",
    )
    def _check_blocking_duplicates(self):
        """Raise ValidationError when a blocking duplicate rule (1–4) matches."""
        for loc in self:
            if not loc.active:
                continue
            result = loc.sudo()._evaluate_duplicates()
            if result["is_blocking"]:
                other = self.browse(result["duplicate_of_id"])
                raise ValidationError(
                    "A duplicate location already exists:\n\n"
                    "%s\n\n"
                    "This record cannot be saved because it matches an existing "
                    "location. Please use the existing location instead, or verify "
                    "that this is genuinely a different location." % (
                        other.location_display_label or other.name
                    )
                )

    @api.onchange(
        "google_place_id", "chain_name", "location_number", "business_name",
        "branch_name", "unit", "address", "street", "city", "postal_code",
        "country_id",
    )
    def _onchange_duplicate_check(self):
        """Live duplicate feedback in the form view — sets duplicate_status
        and duplicate_of_id on the in-memory record so the banner shows."""
        if not self.address:
            return
        # Compute normalized fields on-the-fly for a new record
        self.location_number_normalized = self._normalize_location_number(self.location_number)
        self.normalized_brand = self._normalize_business(self.chain_name)
        self.normalized_business = self._normalize_business(self.business_name)
        self.normalized_branch = self._normalize_business(self.branch_name)
        self.normalized_unit = self._normalize_unit(self.unit)
        raw_addr = " ".join(p for p in [
            self.street, self.street2, self.city, self.province_code,
            self._normalize_postal(self.postal_code),
            (self.country_id.code or self.country_id.name or ""),
        ] if p)
        self.normalized_address = self._normalize_address_street(raw_addr or self.address)

        result = self._evaluate_duplicates()
        self.duplicate_status = result["duplicate_status"]
        self.duplicate_of_id = result["duplicate_of_id"]

    # ═══════════════════════════════════════════════════════════════════════
    # CRUD Overrides
    # ═══════════════════════════════════════════════════════════════════════

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            if rec.address and not rec.address_validated:
                rec._validate_address()
            # Compute duplicate status (skipped if context says internal)
            if not self.env.context.get("_location_internal_write"):
                rec._update_duplicate_status()
        return records

    def write(self, vals):
        # ── Detect manual address edits on Google-verified records ──
        address_field_keys = {
            "address", "street", "street2", "unit", "city",
            "province_code", "postal_code", "country_id",
        }
        is_address_edit = bool(address_field_keys & set(vals.keys()))
        # If the caller is explicitly setting google_place_id / google_verified,
        # it's a Google Places selection — don't clear those flags.
        is_google_selection = ("google_place_id" in vals or "google_verified" in vals)

        # ── If this is a post-processing (internal) write, skip the overrides ──
        if self.env.context.get("_location_internal_write"):
            return super().write(vals)

        res = super().write(vals)

        for rec in self:
            updates = {}
            # Clear Google verification on manual address edit
            if is_address_edit and not is_google_selection:
                if rec.google_verified and rec.google_place_id:
                    updates["google_place_id"] = False
                    updates["google_verified"] = False

            if updates:
                rec.with_context(_location_internal_write=True).write(updates)

        # Update duplicate status for affected records
        for rec in self:
            rec._update_duplicate_status()

        return res

    def _update_duplicate_status(self):
        """Persist duplicate_status and duplicate_of_id after a change.
        Called from create / write / scan.  Uses internal-write context to
        avoid recursion."""
        self.ensure_one()
        result = self.sudo()._evaluate_duplicates()
        current = {
            "duplicate_status": self.duplicate_status,
            "duplicate_of_id": self.duplicate_of_id.id,
        }
        if (result["duplicate_status"] != current["duplicate_status"]
                or result["duplicate_of_id"] != current["duplicate_of_id"]):
            self.with_context(_location_internal_write=True).write({
                "duplicate_status": result["duplicate_status"],
                "duplicate_of_id": result["duplicate_of_id"],
            })

    # ═══════════════════════════════════════════════════════════════════════
    # Scan Duplicates (manager action)
    # ═══════════════════════════════════════════════════════════════════════

    def action_scan_duplicates(self):
        """Manager button: recompute normalized fields and duplicate markers
        for all active locations.  Never merges, renames, deletes or archives."""
        active_ids = self.search([("active", "=", True)])
        total = len(active_ids)
        updated = 0
        for loc in active_ids:
            # Recompute stored computed fields
            loc._compute_location_search_fields()
            # Evaluate duplicates
            result = loc.sudo()._evaluate_duplicates()
            if (result["duplicate_status"] != loc.duplicate_status
                    or result["duplicate_of_id"] != loc.duplicate_of_id.id):
                loc.with_context(_location_internal_write=True).write({
                    "duplicate_status": result["duplicate_status"],
                    "duplicate_of_id": result["duplicate_of_id"],
                })
                updated += 1
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Duplicate Scan Complete",
                "message": "%d of %d locations updated." % (updated, total),
                "type": "info",
                "sticky": False,
            },
        }

    # ═══════════════════════════════════════════════════════════════════════
    # Merge (manager action, manual only)
    # ═══════════════════════════════════════════════════════════════════════

    def action_merge_duplicate(self):
        """Merge a duplicate into its primary record (duplicate_of_id).
        Archives the current record, transfers references, preserves notes.

        Only available to Dispatch Managers.  Never overwrites populated
        fields on the primary."""
        self.ensure_one()
        if not self.duplicate_of_id:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Nothing to Merge",
                    "message": "This location has no duplicate target set.",
                    "type": "warning",
                    "sticky": False,
                },
            }

        primary = self.duplicate_of_id
        dup_display = self.location_display_label or self.name

        # Transfer stops (capture count before write empties self.stop_ids)
        transferred_stop_count = len(self.stop_ids)
        self.stop_ids.write({"saved_location_id": primary.id})

        # Transfer extraction audit records
        extractions = self.env["prema.dispatch.location.extraction"].sudo().search([
            ("saved_location_id", "=", self.id),
        ])
        extraction_count = len(extractions)
        extractions.write({"saved_location_id": primary.id})

        # Transfer photos
        photo_recs = self.env["prema.dispatch.location.photo"].sudo().search([
            ("location_id", "=", self.id),
        ])
        photo_count = len(photo_recs)
        photo_recs.write({"location_id": primary.id})

        # Merge identity fields: never overwrite populated fields on primary
        identity_fields = [
            "google_place_id", "chain_name", "location_number",
            "business_name", "branch_name", "unit", "partner_id",
        ]
        primary_updates = {}
        for fname in identity_fields:
            if not getattr(primary, fname) and getattr(self, fname):
                primary_updates[fname] = getattr(self, fname)

        # Merge address components: prefer more complete (longer)
        for fname in ["address", "street", "city", "province_code", "postal_code"]:
            primary_val = getattr(primary, fname) or ""
            dup_val = getattr(self, fname) or ""
            if len(dup_val) > len(primary_val):
                primary_updates[fname] = dup_val

        # Merge pin: use duplicate's pin if primary doesn't have one
        for fname in ["pin_lat", "pin_lng"]:
            if (not getattr(primary, fname) and getattr(self, fname)):
                primary_updates[fname] = getattr(self, fname)
        if not primary.pin_set and self.pin_set:
            primary_updates["pin_set"] = True

        # Merge text fields: never overwrite populated fields on primary
        text_fields = [
            "parking_notes", "driver_instructions", "security_notes",
            "dock_door", "receiving_entrance", "truck_entrance", "gate_code",
        ]
        for fname in text_fields:
            if not getattr(primary, fname) and getattr(self, fname):
                primary_updates[fname] = getattr(self, fname)

        # Merge access flags: True wins
        for fname in [
            "liftgate_required", "dock_height_available", "pump_truck_required",
            "straight_truck_accessible", "trailer_53ft_accessible",
        ]:
            if not getattr(primary, fname) and getattr(self, fname):
                primary_updates[fname] = True

        # Merge location_type: duplicate's type wins if primary is default "customer"
        if primary.location_type == "customer" and self.location_type and self.location_type != "customer":
            primary_updates["location_type"] = self.location_type
        # Merge stop_type: "both" is most permissive, duplicate's type wins if broader
        if self.stop_type == "both" and primary.stop_type != "both":
            primary_updates["stop_type"] = "both"
        elif self.stop_type != "delivery" and primary.stop_type == "delivery":
            primary_updates["stop_type"] = self.stop_type
        if not primary.allow_cross_dock and self.allow_cross_dock:
            primary_updates["allow_cross_dock"] = True

        # Merge photos (binary): copy over only if primary doesn't have one
        photo_fields = [
            "entrance_photo", "dock_photo", "parking_photo", "gate_photo",
            "receiving_office_photo", "scale_photo", "loading_area_photo",
        ]
        for fname in photo_fields:
            if not getattr(primary, fname) and getattr(self, fname):
                primary_updates[fname] = getattr(self, fname)

        # Merge geofence: keep larger radius
        if self.geofence_radius and self.geofence_radius > primary.geofence_radius:
            primary_updates["geofence_radius"] = self.geofence_radius

        # Recompute visit count from actual stops (after transfer)
        actual_visits = self.env["prema.dispatch.stop"].sudo().search_count([
            ("saved_location_id", "=", primary.id),
        ])
        primary_updates["use_count"] = actual_visits

        # Preserve newest last_visited
        primary_last = primary.last_visited or False
        dup_last = self.last_visited or False
        if dup_last and (not primary_last or dup_last > primary_last):
            primary_updates["last_visited"] = dup_last

        if primary_updates:
            primary.with_context(_location_internal_write=True).write(primary_updates)

        # Archive duplicate, keep duplicate_of_id pointing to primary
        self.with_context(_location_internal_write=True).write({
            "active": False,
            "duplicate_status": "clean",
            "duplicate_of_id": primary.id,
        })

        # Add chatter note on primary
        note_body = (
            "🔗 <strong>Merged duplicate:</strong> %s (ID #%d)<br/>"
            "<ul>"
            "<li>Stops transferred: %d</li>"
            "<li>Extractions transferred: %d</li>"
            "<li>Photos transferred: %d</li>"
            "<li>Visits recomputed: %d</li>"
            "</ul>"
        ) % (dup_display, self.id,
             transferred_stop_count,
             extraction_count, photo_count,
             primary.use_count)
        primary.message_post(body=note_body, message_type="comment")

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Merge Complete",
                "message": "Merged into: %s" % (primary.location_display_label or primary.name),
                "type": "info",
                "sticky": False,
            },
        }

    # ═══════════════════════════════════════════════════════════════════════
    # Possible Matches (smart button)
    # ═══════════════════════════════════════════════════════════════════════

    def action_find_possible_matches(self):
        """Smart button: search for possible matches using Google Place,
        normalized address, brand, store number, and business similarity."""
        self.ensure_one()
        domain_parts = []
        if self.google_place_id:
            domain_parts.append([("google_place_id", "=", self.google_place_id)])
        if self.normalized_address:
            domain_parts.append([("normalized_address", "=", self.normalized_address)])
        if self.normalized_brand:
            domain_parts.append([("normalized_brand", "=", self.normalized_brand)])
        if self.location_number_normalized:
            domain_parts.append([("location_number_normalized", "=", self.location_number_normalized)])
        if self.normalized_business:
            domain_parts.append([("normalized_business", "=", self.normalized_business)])
        if not domain_parts:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "No Search Criteria",
                    "message": "Fill in address, brand, or business fields first.",
                    "type": "warning",
                    "sticky": False,
                },
            }
        domain = expression.OR(domain_parts)
        domain = expression.AND([domain, [("id", "!=", self.id), ("active", "=", True)]])
        return {
            "type": "ir.actions.act_window",
            "name": "Possible Matches",
            "res_model": "prema.dispatch.location",
            "view_mode": "list,form",
            "domain": domain,
            "target": "current",
        }

    # ═══════════════════════════════════════════════════════════════════════
    # Existing Methods (unchanged logic)
    # ═══════════════════════════════════════════════════════════════════════

    def _driver_payload(self):
        self.ensure_one()
        photos = self.env["prema.dispatch.location.photo"].sudo().search([
            ("location_id", "=", self.id), ("active", "=", True),
        ])
        entrance_photos = photos.filtered(lambda p: p.photo_type in ("entrance", "truck_entrance"))
        primary_entrance = entrance_photos.filtered("is_primary")[:1] or entrance_photos[:1]
        if primary_entrance:
            primary_entrance_photo_url = f"/web/image/ir.attachment/{primary_entrance.attachment_id.id}/datas"
        elif self.entrance_photo:
            primary_entrance_photo_url = f"/web/image/prema.dispatch.location/{self.id}/entrance_photo"
        else:
            primary_entrance_photo_url = ""
        return {
            "id": self.id, "display_label": self.location_display_label or self.display_name,
            "chain_name": self.chain_name or "", "location_number": self.location_number or "",
            "business_name": self.business_name or "", "address": self.address or "",
            "city": self.city or "", "postal_code": self.postal_code or "", "location_type": self.location_type or "",
            "stop_type": self.stop_type or "delivery",
            "usage_type": self.usage_type or "unknown",
            "dock_door": self.dock_door or "", "pin_lat": self.pin_lat, "pin_lng": self.pin_lng,
            "pin_source": self.pin_source or "", "exact_pin_available": bool(self.pin_set),
            "verification_state": self.verification_state or "",
            "primary_entrance_photo_url": primary_entrance_photo_url,
            "photos": [
                {"id": p.id, "photo_type": p.photo_type, "url": f"/web/image/ir.attachment/{p.attachment_id.id}/datas"}
                for p in photos
            ],
            "parking_notes": self.parking_notes or "", "driver_instructions": self.driver_instructions or "",
        }

    @api.model
    def driver_search_locations(self, query, limit=20, offset=0):
        query = (query or "").strip()
        limit = min(int(limit or 20), 50)
        offset = max(int(offset or 0), 0)
        if len(query) < 2:
            return {"success": True, "results": [], "limit": limit, "offset": offset}
        norm = self._normalize_search_token(query)
        number = self._normalize_location_number(query.split()[-1])
        words = self._normalize_search_words(query)
        results = self.browse()
        if len(words) >= 2:
            chain_guess = " ".join(words[:-1])
            results |= self.search([("chain_name", "=ilike", chain_guess), ("location_number_normalized", "=", words[-1])], limit=limit)
        if number:
            results |= self.search([("location_number_normalized", "=", number)], limit=limit)
        for field in ("google_place_id", "normalized_address", "business_name", "name", "city", "postal_code", "address", "location_search_key"):
            if len(results) >= limit + offset:
                break
            op = "=" if field in ("google_place_id", "normalized_address") else "ilike"
            results |= self.search([(field, op, norm if field in ("normalized_address", "location_search_key") else query)], limit=limit)
        if len(results) < limit + offset and len(words) >= 2:
            word_domain = [("location_search_key", "ilike", word) for word in words]
            results |= self.search(word_domain, limit=limit)
        sliced = results[offset:offset + limit]
        return {"success": True, "results": [r._driver_payload() for r in sliced], "limit": limit, "offset": offset}

    @api.depends("stop_ids.stop_type")
    def _compute_usage_type(self):
        """Compute whether this location is used for pickups, deliveries, or both.

        Based on the stop_type of all linked stops:
        - pickup / cross_dock_pickup → counts as pickup
        - dropoff / return / cross_dock_drop / transfer → counts as delivery
        """
        PICKUP_TYPES = {"pickup", "cross_dock_pickup"}
        DELIVERY_TYPES = {"dropoff", "return", "cross_dock_drop", "transfer"}
        for loc in self:
            types = set()
            for stop in loc.stop_ids:
                if stop.stop_type in PICKUP_TYPES:
                    types.add("pickup")
                elif stop.stop_type in DELIVERY_TYPES:
                    types.add("delivery")
            if "pickup" in types and "delivery" in types:
                loc.usage_type = "both"
            elif "pickup" in types:
                loc.usage_type = "pickup"
            elif "delivery" in types:
                loc.usage_type = "delivery"
            else:
                loc.usage_type = "unknown"

    @api.depends("stop_ids")
    def _compute_stop_count(self):
        for loc in self:
            loc.stop_count = len(loc.stop_ids)

    def name_get(self):
        """Show the smart display label (Business — City) everywhere this
        location is referenced — form title, list view, stop pickers, etc.

        Falls back through: display_label → business_name → name → address."""
        return [
            (
                rec.id,
                rec.location_display_label or rec.business_name or rec.name or rec.address or "",
            )
            for rec in self
        ]

    @api.model
    def name_search(self, name="", args=None, operator="ilike", limit=100):
        """Many2one autocomplete (stop pickers etc.) — same normalized
        word-AND matching as the list-view search box, so partial and
        multi-word queries behave consistently everywhere."""
        args = list(args or [])
        if name:
            if operator.endswith("like"):
                args = expression.AND([
                    args, self._search_location_anywhere(operator, name),
                ])
            else:
                # Non-like operators (e.g. '=' quick-create lookups): exact
                # match on the location name.
                args = expression.AND([args, [("name", operator, name)]])
        return self.search(args, limit=limit).name_get()

    def _validate_address(self):
        """Check address accuracy via Google's Address Validation API — same
        pattern as prema.dispatch.stop._validate_address(). Does not rewrite
        the address, only flags/stores the standardized form for reference."""
        self.ensure_one()
        api_key = self.env["ir.config_parameter"].sudo().get_param("google_maps_api_key")
        if not api_key or not self.address:
            return False
        try:
            import requests
            r = requests.post(
                "https://addressvalidation.googleapis.com/v1:validateAddress",
                params={"key": api_key},
                json={"address": {"regionCode": "CA", "addressLines": [self.address]}},
                timeout=5,
            )
            data = r.json()
            result = data.get("result")
            if result is None:
                return False
            verdict = result.get("verdict", {})
            warning = ""
            if not verdict.get("addressComplete", True):
                warning = "Address looks incomplete"
            elif verdict.get("hasUnconfirmedComponents"):
                warning = "Some address details could not be confirmed"
            elif verdict.get("hasReplacedComponents"):
                warning = "Address auto-corrected — please verify"
            self.write({
                "address_validated": True,
                "address_validation_warning": warning,
                "address_formatted": result.get("address", {}).get("formattedAddress", ""),
            })
            return True
        except Exception:
            _logger.exception("Address validation failed for location %s (%r)", self.id, self.address)
            return False

    @api.model
    def find_or_create_by_address(self, address, place_id=None, business_name=None, partner_id=None):
        """Return an existing saved location matching address/place_id, or create one.

        Matching order: Google place_id (most precise) → exact address text
        → same business_name + partner (catches the same shipper re-entered
        with slightly different address text, e.g. two pickups from the same
        warehouse). This is what lets a repeat stop reuse the saved pin,
        notes, and photo instead of starting from scratch every time.

        search() applies Odoo's default active_test, so only active=True
        locations are matched/reused here — an archived location will not
        be silently reattached to new stops.
        """
        if not address:
            return self.browse()

        existing = self.browse()
        if place_id:
            existing = self.search([("google_place_id", "=", place_id)], limit=1)
        if not existing:
            existing = self.search([("address", "=ilike", address.strip())], limit=1)
        if not existing and business_name and partner_id:
            existing = self.search([
                ("business_name", "=ilike", business_name.strip()),
                ("partner_id", "=", partner_id),
            ], limit=1)
        if existing:
            return existing

        return self.create({
            "name":           business_name or address[:80],
            "business_name":  business_name or "",
            "address":        address,
            "google_place_id": place_id or "",
            "partner_id":     partner_id or False,
        })

    def update_pin(self, lat, lng, source="dispatcher"):
        """Update the parking pin position and mark as manually set."""
        self.ensure_one()
        self.write({
            "pin_lat": lat,
            "pin_lng": lng,
            "pin_set": True,
        })
        return True

    def record_visit(self):
        """Increment visit counter and update last-visited timestamp."""
        self.ensure_one()
        self.write({
            "use_count":    self.use_count + 1,
            "last_visited": fields.Datetime.now(),
        })

    def record_visit_stats(self, stop):
        """Roll a just-completed stop's timing into this location's running
        averages and heuristic scores. Called from the stop-completion path
        (prema.dispatch.stop.action_mark_completed / dispatch_job.driver_update_stop)
        when the stop is linked to a saved location.

        NOTE: these are simple running-average heuristics, not real ML —
        good enough to nudge dispatchers, not to be treated as guaranteed.
        """
        self.ensure_one()
        if not stop:
            return False

        arrival = stop.actual_arrival_time
        departure = stop.actual_departure_time
        total_minutes = None
        if arrival and departure:
            total_minutes = (departure - arrival).total_seconds() / 60.0

        unload_minutes = stop.service_time_minutes or None

        # Compliance: was the arrival within the requested window/appointment?
        target_time = stop.exact_time or stop.earliest_time or stop.latest_time or stop.scheduled_time
        delay_minutes = None
        on_time = None
        if arrival and target_time:
            delay_minutes = (arrival - target_time).total_seconds() / 60.0
            if stop.exact_time:
                on_time = abs(delay_minutes) <= 15
            elif stop.latest_time:
                on_time = arrival <= stop.latest_time
            elif stop.earliest_time:
                on_time = arrival >= stop.earliest_time

        # Running averages — weight the new sample against use_count (already
        # incremented by record_visit() before this is normally called, so
        # guard against divide-by-zero on the very first visit).
        n = max(self.use_count, 1)
        prev_n = max(n - 1, 0)

        def rolling_avg(prev_avg, new_value):
            if new_value is None:
                return prev_avg
            if prev_n <= 0:
                return new_value
            return ((prev_avg * prev_n) + new_value) / n

        vals = {}

        if total_minutes is not None:
            vals["average_total_stop_minutes"] = rolling_avg(self.average_total_stop_minutes, total_minutes)
            vals["fastest_stop_minutes"] = (
                total_minutes if not self.fastest_stop_minutes
                else min(self.fastest_stop_minutes, total_minutes)
            )
            vals["longest_stop_minutes"] = max(self.longest_stop_minutes or 0.0, total_minutes)

        if unload_minutes is not None:
            vals["average_unload_minutes"] = rolling_avg(self.average_unload_minutes, unload_minutes)

        if total_minutes is not None and unload_minutes is not None:
            wait_minutes = max(total_minutes - unload_minutes, 0.0)
            vals["average_wait_minutes"] = rolling_avg(self.average_wait_minutes, wait_minutes)

        if delay_minutes is not None:
            vals["average_delay_minutes"] = rolling_avg(self.average_delay_minutes, delay_minutes)

        if on_time is not None:
            prev_compliance = self.appointment_compliance_pct or 0.0
            prev_hits = round(prev_compliance / 100.0 * prev_n)
            new_hits = prev_hits + (1 if on_time else 0)
            vals["appointment_compliance_pct"] = (new_hits / n) * 100.0

        if delay_minutes is not None:
            # Arrival accuracy: how close to the target time, as a 0-100 score
            # that decays with growing lateness/earliness (0 at 60+ min off).
            accuracy_sample = max(0.0, 100.0 - (abs(delay_minutes) / 60.0) * 100.0)
            vals["arrival_accuracy_pct"] = rolling_avg(self.arrival_accuracy_pct, accuracy_sample)

        # Recommended service time: rounded average unload time, falling back
        # to whatever we already had.
        if "average_unload_minutes" in vals and vals["average_unload_minutes"]:
            vals["recommended_service_time_minutes"] = int(round(vals["average_unload_minutes"]))

        # Best/avoid arrival windows: bucket this arrival's hour as "good" if
        # it was on-time/low-delay, "avoid" if it was late — a running signal,
        # not a full statistical model.
        if arrival:
            hour = arrival.hour
            window_start = f"{hour:02d}:00"
            window_end = f"{(hour + 1) % 24:02d}:00"
            if on_time is False or (delay_minutes is not None and delay_minutes > 30):
                vals["avoid_arrival_window_start"] = window_start
                vals["avoid_arrival_window_end"] = window_end
            elif on_time or (delay_minutes is not None and delay_minutes <= 0):
                vals["best_arrival_window_start"] = window_start
                vals["best_arrival_window_end"] = window_end

        # Heuristic scores (0-100, higher delay/non-compliance = higher difficulty).
        avg_delay = vals.get("average_delay_minutes", self.average_delay_minutes) or 0.0
        compliance = vals.get("appointment_compliance_pct", self.appointment_compliance_pct) or 0.0
        vals["difficulty_score"] = max(0.0, min(100.0, (max(avg_delay, 0.0) * 1.5) + (100.0 - compliance) * 0.5))

        avg_unload = vals.get("average_unload_minutes", self.average_unload_minutes) or 0.0
        if avg_unload:
            # Faster than a 30-min baseline unload = more efficient dock.
            vals["dock_efficiency_score"] = max(0.0, min(100.0, 100.0 - ((avg_unload - 30.0) / 30.0) * 100.0))

        avg_wait = vals.get("average_wait_minutes", self.average_wait_minutes) or 0.0
        vals["traffic_impact_score"] = max(0.0, min(100.0, (avg_wait / 60.0) * 100.0))

        # Confidence scales with sample size, not accuracy — heuristic only.
        vals["ai_confidence_score"] = min(100.0, self.use_count * 10)

        if vals:
            self.write(vals)

        # Phase 6: archive the raw sample so median / last-10 / per-type
        # statistics are exact (computed from the sample table, not the
        # rolling averages above).
        if total_minutes is not None:
            self.env["prema.dispatch.location.visit.sample"].create({
                "location_id":     self.id,
                "stop_id":         stop.id,
                "visited_at":      arrival or fields.Datetime.now(),
                "stop_type":       stop.stop_type,
                "dwell_minutes":   total_minutes,
                "service_minutes": unload_minutes or 0.0,
                "wait_minutes":    max(total_minutes - (unload_minutes or 0.0), 0.0),
            })
            self._recompute_sample_stats()
        return True

    def _recompute_sample_stats(self):
        """Median dwell, last-10 dwell average, per-type loading/unloading
        averages — exact figures over the raw visit-sample table (Phase 6)."""
        self.ensure_one()
        import statistics
        ordered = self.visit_sample_ids.sorted("id")  # chronological
        dwells = [s.dwell_minutes for s in ordered if s.dwell_minutes]
        vals = {}
        if dwells:
            vals["median_dwell_minutes"] = statistics.median(sorted(dwells))
            recent = dwells[-10:]  # most recent 10 samples
            vals["avg_last10_dwell_minutes"] = sum(recent) / len(recent)
        load_svc = [s.service_minutes for s in ordered
                    if s.is_loading and s.service_minutes]
        unload_svc = [s.service_minutes for s in ordered
                      if s.is_unloading and s.service_minutes]
        if load_svc:
            vals["avg_loading_minutes"] = sum(load_svc) / len(load_svc)
        if unload_svc:
            vals["avg_unloading_minutes"] = sum(unload_svc) / len(unload_svc)
        if vals:
            self.write(vals)


class PremaDispatchLocationVisitSample(models.Model):
    _name = "prema.dispatch.location.visit.sample"
    _description = "Saved-Location Visit Timing Sample"
    _order = "visited_at asc, id asc"

    location_id = fields.Many2one(
        "prema.dispatch.location", required=True, ondelete="cascade",
        index=True,
    )
    stop_id = fields.Many2one("prema.dispatch.stop", ondelete="set null")
    visited_at = fields.Datetime(required=True)
    stop_type = fields.Char(string="Stop Type")
    dwell_minutes = fields.Float(string="Dwell (min)")
    service_minutes = fields.Float(string="Service (min)")
    wait_minutes = fields.Float(string="Wait (min)")
    is_loading = fields.Boolean(
        compute="_compute_sample_kind", string="Loading",
        store=False,
    )
    is_unloading = fields.Boolean(
        compute="_compute_sample_kind", string="Unloading",
        store=False,
    )

    def _compute_sample_kind(self):
        for s in self:
            s.is_loading = s.stop_type == "pickup"
            s.is_unloading = s.stop_type in ("dropoff", "return")

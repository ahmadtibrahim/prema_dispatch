"""Customer Saved Location — commercial-layer address book.

Each location belongs to a commercial_partner_id. Portal users see only
their own company's locations. Internal dispatch.location records are
created/updated as execution-layer mirrors.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

_logger = logging.getLogger(__name__)


def _valid_coordinate_pair(latitude, longitude):
    """True only for a usable physical coordinate pair.

    A pair is usable when both values are within valid ranges AND it is
    not the 0.0/0.0 placeholder. A legitimate coordinate with one exact
    zero component (e.g. the Equator) is accepted.
    """
    if latitude is None or longitude is None:
        return False
    lat, lng = float(latitude), float(longitude)
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
        return False
    if lat == 0.0 and lng == 0.0:
        return False
    return True


class LogisticsSavedLocation(models.Model):
    _name = "logistics.saved.location"
    _description = "Customer Saved Location"
    _inherit = ["mail.thread"]
    _order = "is_default_pickup DESC, is_default_delivery DESC, last_used_date DESC, name"
    _rec_name = "name"

    # ── Identity ──────────────────────────────────────────────────────
    name = fields.Char(string="Location Name", required=True)
    commercial_partner_id = fields.Many2one(
        "res.partner", string="Customer Account", required=True, index=True,
        domain="[('is_company', '=', True)]",
        help="The commercial company account that owns this location.",
    )
    company_name = fields.Char(string="Company Name")
    chain_name = fields.Char(string="Chain / Brand", index=True,
        help="Retail chain or brand at this location (e.g. Foodland, Metro).")
    business_name = fields.Char(string="Business Name",
        help="Operating business name (e.g. Foodland).")
    branch_name = fields.Char(string="Branch Name",
        help="Branch or display name (e.g. Picton). Auto-defaults to [Chain] - [City].")
    branch_name_manual = fields.Boolean(string="Branch Name Manually Set", default=False,
        help="True once the user has manually edited the branch name. "
             "Auto-default is suppressed while this is set.")
    store_number = fields.Char(string="Store / Location #", index=True,
        help="Store number or location ID (e.g. 3290).")
    contact_name = fields.Char(string="Contact Name")
    contact_phone = fields.Char(string="Contact Phone")
    contact_email = fields.Char(string="Contact Email")

    # ── Location Type ─────────────────────────────────────────────────
    location_type = fields.Selection([
        ("pickup", "Pickup Location"),
        ("delivery", "Delivery Location"),
        ("both", "Pickup and Delivery"),
    ], string="Location Type", required=True, default="pickup")

    # ── Address ───────────────────────────────────────────────────────
    street = fields.Char(string="Street")
    street2 = fields.Char(string="Street 2")
    unit = fields.Char(string="Unit / Suite")
    city = fields.Char(string="City")
    postal_code = fields.Char(string="Postal Code")
    country_id = fields.Many2one(
        "res.country", string="Country",
        default=lambda self: self.env.ref("base.ca"),
    )
    state_id = fields.Many2one(
        "res.country.state", string="Province / State",
        domain="[('country_id', '=', country_id)]",
    )
    formatted_address = fields.Char(
        string="Formatted Address", readonly=True,
        help="Full address as returned by Google Places.",
    )

    # ── Google ────────────────────────────────────────────────────────
    google_place_id = fields.Char(
        string="Google Place ID", index=True, copy=False,
        help="Unique Google Places identifier for this address.",
    )
    latitude = fields.Float(string="Latitude", digits=(10, 6))
    longitude = fields.Float(string="Longitude", digits=(10, 6))
    google_verified = fields.Boolean(
        string="Google Verified", default=False,
        help="Address was validated through Google Places.",
    )
    google_verified_at = fields.Datetime(
        string="Google Verified At", readonly=True, copy=False,
    )

    # ── Region Detection ──────────────────────────────────────────────
    detected_region_id = fields.Many2one(
        "logistics.region", string="Detected Region", readonly=True,
        help="Service region determined by RegionResolver.",
    )
    region_match_result = fields.Selection([
        ("SCHEDULED_MATCH", "Scheduled Network"),
        ("MANUAL_QUOTE", "Manual Quote Required"),
        ("NETWORK_DISABLED", "Not Available for Automatic Online Booking"),
        ("AMBIGUOUS", "Manual Region Review Required"),
    ], string="Region Status", readonly=True)
    region_match_method = fields.Char(string="Matching Method", readonly=True)
    region_match_timestamp = fields.Datetime(string="Region Matched At", readonly=True)
    region_boundary_version = fields.Char(
        string="Boundary Version", readonly=True,
        help="Checksum of the region boundary used for matching.",
    )
    manual_quote_required = fields.Boolean(
        string="Manual Quote Required", readonly=True,
        help="Address is outside scheduled service corridors.",
    )
    candidate_regions = fields.Text(
        string="Candidate Regions", readonly=True,
        help="JSON list of region codes if multiple matched.",
    )

    # ── Region Override (staff) ───────────────────────────────────────
    override_region_id = fields.Many2one(
        "logistics.region", string="Override Region",
        help="Staff-assigned region when automatic detection is insufficient.",
    )
    override_user_id = fields.Many2one(
        "res.users", string="Override By", readonly=True, copy=False,
    )
    override_date = fields.Datetime(string="Override Date", readonly=True, copy=False)
    override_reason = fields.Text(string="Override Reason")

    # ── Operational Details ───────────────────────────────────────────
    pickup_instructions = fields.Text(string="Pickup Instructions")
    delivery_instructions = fields.Text(string="Delivery Instructions")
    timezone = fields.Char(
        string="Timezone", default="America/Toronto",
        help="IANA timezone from Google Time Zone API. All hours in local time.",
    )
    hours_status = fields.Selection([
        ("configured", "Configured"), ("not_configured", "Hours Not Configured"),
    ], default="not_configured", string="Hours Status")
    hours_ids = fields.One2many(
        "logistics.saved.location.hours", "saved_location_id",
        string="Operating Hours",
        help="Structured weekly operating hours — the scheduling authority "
             "for ETA calculation (general/pickup/receiving scopes).",
    )
    hours_summary = fields.Char(
        string="Hours Summary", compute="_compute_hours_summary", store=False,
        help="Compact weekly schedule for list columns: 'Open 24h', "
             "'Mon–Fri 08:00–17:00', 'Closed' or 'See hours'.",
    )
    dock_info = fields.Char(string="Dock Info")
    opening_hours = fields.Char(string="Opening Hours")
    receiving_hours = fields.Char(string="Receiving Hours")
    shipping_hours = fields.Char(string="Shipping Hours")
    appointment_required = fields.Boolean(string="Appointment Required")
    appointment_contact = fields.Char(string="Appointment Contact")
    appointment_phone = fields.Char(string="Appointment Phone")
    liftgate_required = fields.Boolean(string="Liftgate Required")
    forklift_available = fields.Boolean(string="Forklift Available")

    # ── Defaults ──────────────────────────────────────────────────────
    is_default_pickup = fields.Boolean(
        string="Default Pickup", default=False,
        help="The default pickup location for this customer account.",
    )
    is_default_delivery = fields.Boolean(
        string="Default Delivery", default=False,
        help="The default delivery location for this customer account.",
    )

    # ── System ────────────────────────────────────────────────────────
    active = fields.Boolean(default=True)
    last_used_date = fields.Datetime(string="Last Used", readonly=True)
    dispatch_location_id = fields.Many2one(
        "prema.dispatch.location", string="Dispatch Location",
        readonly=False, ondelete="set null", copy=False,
        help="Linked internal dispatch location. Auto-created if left empty; "
             "set directly when selecting a shared master facility.",
    )

    # ── Constraints ───────────────────────────────────────────────────
    _sql_constraints = [
        ("unique_place_partner_unit",
         "UNIQUE(google_place_id, commercial_partner_id, unit)",
         "This address already exists for this customer. "
         "Consider changing the location type to 'Pickup and Delivery' instead."),
    ]

    # ── Default management ────────────────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._manage_defaults_on_create()
        try:
            records._sync_dispatch_location()
        except Exception:
            _logger.warning("Failed to sync dispatch location for saved location", exc_info=True)
        return records

    def write(self, vals):
        result = super().write(vals)
        if any(f in vals for f in ("location_type", "active", "is_default_pickup",
                                    "is_default_delivery")):
            self._manage_defaults_on_write(vals)
        if any(f in vals for f in ("name", "street", "street2", "unit", "city",
                                    "postal_code", "country_id", "state_id",
                                    "latitude", "longitude", "google_place_id",
                                    "formatted_address", "contact_name",
                                    "contact_phone", "contact_email",
                                    "dock_info", "pickup_instructions",
                                    "delivery_instructions", "liftgate_required",
                                    "forklift_available", "opening_hours")):
            self._sync_dispatch_location()
        return result

    def _manage_defaults_on_create(self):
        """Set first pickup/delivery location as default automatically."""
        for rec in self:
            if rec.commercial_partner_id:
                if rec._is_pickup_capable():
                    existing_default = self.search([
                        ("commercial_partner_id", "=", rec.commercial_partner_id.id),
                        ("is_default_pickup", "=", True),
                        ("active", "=", True),
                        ("id", "!=", rec.id),
                    ])
                    if not existing_default:
                        rec.is_default_pickup = True

                if rec._is_delivery_capable():
                    existing_default = self.search([
                        ("commercial_partner_id", "=", rec.commercial_partner_id.id),
                        ("is_default_delivery", "=", True),
                        ("active", "=", True),
                        ("id", "!=", rec.id),
                    ])
                    if not existing_default:
                        rec.is_default_delivery = True

    def _manage_defaults_on_write(self, vals):
        """Clear invalid defaults on type change. Auto-set defaults when
        the location becomes newly eligible and no other default exists."""
        for rec in self:
            partner_id = rec.commercial_partner_id.id
            if not partner_id:
                continue

            loc_type_changed = "location_type" in vals

            # Clear invalid defaults based on type
            if loc_type_changed and not self._is_pickup_capable():
                rec.is_default_pickup = False
            if loc_type_changed and not self._is_delivery_capable():
                rec.is_default_delivery = False

            # Auto-set default when type changes to become eligible
            # and no other default exists (same logic as _manage_defaults_on_create)
            if loc_type_changed and self._is_pickup_capable():
                existing_pu = self.search([
                    ("commercial_partner_id", "=", partner_id),
                    ("is_default_pickup", "=", True),
                    ("active", "=", True),
                    ("id", "!=", rec.id),
                ])
                if not existing_pu and not rec.is_default_pickup:
                    rec.is_default_pickup = True

            if loc_type_changed and self._is_delivery_capable():
                existing_de = self.search([
                    ("commercial_partner_id", "=", partner_id),
                    ("is_default_delivery", "=", True),
                    ("active", "=", True),
                    ("id", "!=", rec.id),
                ])
                if not existing_de and not rec.is_default_delivery:
                    rec.is_default_delivery = True

            # Enforce single default pickup
            if vals.get("is_default_pickup"):
                self.search([
                    ("commercial_partner_id", "=", partner_id),
                    ("is_default_pickup", "=", True),
                    ("active", "=", True),
                    ("id", "!=", rec.id),
                ]).write({"is_default_pickup": False})

            # Enforce single default delivery
            if vals.get("is_default_delivery"):
                self.search([
                    ("commercial_partner_id", "=", partner_id),
                    ("is_default_delivery", "=", True),
                    ("active", "=", True),
                    ("id", "!=", rec.id),
                ]).write({"is_default_delivery": False})

            # Clear default on archive
            if vals.get("active") is False:
                rec.is_default_pickup = False
                rec.is_default_delivery = False

    def _is_pickup_capable(self):
        self.ensure_one()
        return self.location_type in ("pickup", "both")

    def _is_delivery_capable(self):
        self.ensure_one()
        return self.location_type in ("delivery", "both")

    # ── Canonical physical coordinates ──────────────────────────────

    def _get_effective_coordinates(self):
        """Canonical physical coordinates for this saved location.

        Priority:
          1. linked Master Facility's valid pin pair (physical authority)
          2. this record's own latitude/longitude
          3. no coordinates

        Booking/portal code MUST use this helper — never raw fields — so a
        stale 0,0 customer copy can never break a booking when its master
        has the real coordinates.
        """
        self.ensure_one()
        master = self.dispatch_location_id
        if master and _valid_coordinate_pair(master.pin_lat, master.pin_lng):
            return {
                "latitude": master.pin_lat,
                "longitude": master.pin_lng,
                "source": "master_pin",
            }
        if _valid_coordinate_pair(self.latitude, self.longitude):
            return {
                "latitude": self.latitude,
                "longitude": self.longitude,
                "source": "saved_location",
            }
        return {"latitude": None, "longitude": None, "source": "none"}

    def _sync_physical_from_master(self):
        """Pull shared PHYSICAL data from the linked master into the copy.

        Synchronizes latitude/longitude (and Google identity when the
        master has it) from prema.dispatch.location onto this record.
        NEVER touches customer-private fields: contact_name/phone/email,
        pickup/delivery instructions, notes, customer alias, hours.
        Only writes when the master has a valid coordinate pair.
        """
        for rec in self:
            master = rec.dispatch_location_id
            if not master or not _valid_coordinate_pair(master.pin_lat, master.pin_lng):
                continue
            vals = {}
            if rec.latitude != master.pin_lat or rec.longitude != master.pin_lng:
                vals.update({
                    "latitude": master.pin_lat,
                    "longitude": master.pin_lng,
                })
            if master.google_place_id and rec.google_place_id != master.google_place_id:
                vals["google_place_id"] = master.google_place_id
            if master.google_verified and not rec.google_verified:
                vals["google_verified"] = True
            if vals:
                rec.write(vals)

    # ── Google Places batch verification ────────────────────────────

    def action_google_verify_locations(self):
        """Batch Google-verify unverified saved locations with usable addresses.
        Uses Google Places API to resolve place IDs, business names, and
        standardized addresses. Updates display names to 'Business - City'.
        Returns a verification report."""
        import json, logging
        _logger = logging.getLogger(__name__)
        ICP = self.env["ir.config_parameter"].sudo()
        api_key = ICP.get_param("google_maps_api_key", "")
        if not api_key:
            return {
                "type": "ir.actions.client", "tag": "display_notification",
                "params": {"type": "danger", "message": "Google Maps API key not configured."},
            }

        # Process locations with usable addresses — unverified OR address-like biz names
        candidates = self.search([
            ("street", "!=", False), ("street", "!=", ""),
            ("city", "!=", False), ("city", "!=", ""),
            ("active", "=", True),
        ])
        # Filter: unverified OR business_name looks like an address (starts with digit)
        def _needs_verify(loc):
            if not loc.google_verified:
                return True
            biz = (loc.business_name or "").strip()
            return biz and biz[:1].isdigit()  # address-like business name
        domain_ids = [loc.id for loc in candidates if _needs_verify(loc)]
        total = len(domain_ids)
        if total == 0:
            return {
                "type": "ir.actions.client", "tag": "display_notification",
                "params": {"type": "info", "message": "All locations already verified and named correctly."},
            }

        verified = 0
        names_resolved = 0
        already_ok = 0
        could_not = 0
        duplicates = 0
        report_lines = []

        batch = self.browse(domain_ids)
        for loc in batch:
            try:
                # Build search query: business name if we have one, else address
                query = (loc.business_name or loc.chain_name or "").strip()
                if not query or any(c.isdigit() for c in query[:3]):
                    query = f"{loc.street}, {loc.city}, {loc.province_code or 'ON'}"
                else:
                    query = f"{query}, {loc.street}, {loc.city}"

                # Call Google Places text search
                import urllib.request, urllib.parse
                params = urllib.parse.urlencode({
                    "query": query,
                    "key": api_key,
                    "fields": "places.id,places.displayName,places.formattedAddress,places.location,places.addressComponents",
                })
                url = f"https://places.googleapis.com/v1/places:searchText?{params}"
                # Fallback to legacy Places API if new one fails
                url_fallback = f"https://maps.googleapis.com/maps/api/place/findplacefromtext/json?input={urllib.parse.quote(query)}&inputtype=textquery&fields=place_id,name,formatted_address,geometry&key={api_key}"

                req = urllib.request.Request(url, headers={"X-Goog-FieldMask": "places.id,places.displayName,places.formattedAddress,places.location"})
                try:
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        data = json.loads(resp.read())
                        places = data.get("places", [])
                except Exception:
                    # Fallback to legacy API
                    with urllib.request.urlopen(url_fallback, timeout=5) as resp:
                        data = json.loads(resp.read())
                        places_raw = data.get("candidates", [])
                        places = []
                        for p in places_raw:
                            places.append({
                                "id": p.get("place_id"),
                                "displayName": {"text": p.get("name", "")},
                                "formattedAddress": p.get("formatted_address", ""),
                                "location": p.get("geometry", {}).get("location", {}),
                            })

                if places and places[0].get("id"):
                    place = places[0]
                    pid = place["id"]
                    name = (place.get("displayName") or {}).get("text", "") if isinstance(place.get("displayName"), dict) else place.get("displayName", "")
                    addr = place.get("formattedAddress", "")
                    loc_data = place.get("location", {})
                    lat = loc_data.get("latitude") if isinstance(loc_data, dict) else (loc_data.get("lat") if loc_data else None)
                    lng = loc_data.get("longitude") if isinstance(loc_data, dict) else (loc_data.get("lng") if loc_data else None)

                    vals = {
                        "google_place_id": pid,
                        "google_verified": True,
                        "google_verified_at": fields.Datetime.now(),
                    }
                    if addr and not loc.formatted_address:
                        vals["formatted_address"] = addr
                    if lat and not loc.latitude:
                        vals["latitude"] = lat
                    if lng and not loc.longitude:
                        vals["longitude"] = lng

                    # Resolve business name
                    old_name = loc.name
                    old_business = loc.business_name or ""
                    if name and not loc.business_name:
                        vals["business_name"] = name
                    if name:
                        # Generate friendly display name: Business - City
                        city = loc.city or ""
                        friendly = f"{name} - {city}" if city else name
                        # Only auto-rename if current name looks like an address
                        is_address_like = bool(loc.name) and any(
                            w in (loc.name or "").lower()
                            for w in ["street", " st ", "road", " rd ", "avenue", " ave ",
                                      "boulevard", "blvd", "drive", " dr ", "highway", "hwy",
                                      "unit", "gate", "blvd"]
                        ) or any(c.isdigit() for c in (loc.name or "")[:3])
                        if is_address_like or not loc.name:
                            vals["name"] = friendly
                        # Always set branch_name if blank
                        if not loc.branch_name:
                            vals["branch_name"] = friendly
                        report_lines.append(f"{old_name} → {friendly} [{pid[:16]}...]")
                        names_resolved += 1
                    else:
                        report_lines.append(f"{old_name} → verified [no business name] [{pid[:16]}...]")

                    # Check for duplicates with same place_id
                    dup = self.search([("google_place_id", "=", pid), ("id", "!=", loc.id)], limit=1)
                    if dup:
                        duplicates += 1
                        report_lines.append(f"  ⚠ Duplicate: {dup.name} (ID={dup.id})")

                    # The Master Facility is the physical authority: push
                    # verified coordinates to it FIRST when it lacks a valid
                    # pin pair (manual/dispatcher pins are never clobbered).
                    # The master write hook then syncs this copy automatically.
                    master = loc.dispatch_location_id
                    if master and lat and lng and not _valid_coordinate_pair(
                            master.pin_lat, master.pin_lng):
                        master_vals = {"pin_lat": lat, "pin_lng": lng}
                        if pid and master.google_place_id != pid:
                            master_vals["google_place_id"] = pid
                        if not master.google_verified:
                            master_vals["google_verified"] = True
                        try:
                            master.write(master_vals)
                        except Exception:
                            _logger.warning(
                                "Google verify: could not update master %s for location %s",
                                master.id, loc.id, exc_info=True,
                            )

                    loc.write(vals)
                    verified += 1
                else:
                    report_lines.append(f"{loc.name}: no Google match found")
                    could_not += 1

            except Exception as exc:
                report_lines.append(f"{loc.name}: ERROR — {exc}")
                could_not += 1
                _logger.warning("Google verify failed for location %s: %s", loc.id, exc)

        # Build summary
        summary = (
            f"Google Verification Complete\n"
            f"Total checked: {total}\n"
            f"Successfully Verified: {verified}\n"
            f"Business names resolved: {names_resolved}\n"
            f"Could not verify: {could_not}\n"
            f"Duplicates detected: {duplicates}\n\n"
            + "\n".join(report_lines[:50])
        )
        _logger.info("Google batch verify: %s", summary.replace("\n", " | "))

        return {
            "type": "ir.actions.act_window",
            "name": "Google Verification Report",
            "res_model": "logistics.saved.location",
            "view_mode": "list,form",
            "domain": [("id", "in", batch.ids)],
            "context": {"create": False},
        }

    # ── Region resolution ─────────────────────────────────────────────

    def action_resolve_region(self):
        """Run RegionResolver against this location's coordinates and store result."""
        from ..services.region_resolver import RegionResolver

        resolver = RegionResolver(self.env)
        for rec in self:
            if not rec.latitude or not rec.longitude:
                continue

            result = resolver.resolve(
                latitude=rec.latitude,
                longitude=rec.longitude,
                country=rec.country_id.id if rec.country_id else None,
                state=rec.state_id.id if rec.state_id else None,
            )

            vals = {
                "region_match_result": result.outcome,
                "region_match_method": result.match_method,
                "region_match_timestamp": fields.Datetime.now(),
                "manual_quote_required": result.outcome == "MANUAL_QUOTE",
            }

            if result.matched_region:
                vals["detected_region_id"] = result.matched_region.id
                vals["region_boundary_version"] = (
                    result.matched_region.boundary_checksum or ""
                )

            if result.candidate_regions:
                vals["candidate_regions"] = ", ".join(
                    f"{r.code} ({r.name})" for r in result.candidate_regions
                )

            rec.write(vals)

    def action_resolve_region_with_google(self):
        """Re-validate address via Google Places, then re-resolve region.

        This is the production flow when address fields change.
        Currently stubbed — actual Google Places API integration is
        handled by the address widget in the UI. This method is
        called after Google validation completes.
        """
        self.action_resolve_region()

    # ── Duplicate detection ───────────────────────────────────────────

    @api.model
    def _detect_duplicate(self, commercial_partner_id, google_place_id, unit=None):
        """Check if this customer already has this location.

        Returns existing record or empty recordset.
        """
        if not google_place_id or not commercial_partner_id:
            return self.browse()
        domain = [
            ("commercial_partner_id", "=", commercial_partner_id),
            ("google_place_id", "=", google_place_id),
            ("active", "=", True),
        ]
        if unit:
            domain.append(("unit", "=", unit))
        return self.search(domain, limit=1)

    @staticmethod
    def _merge_location_type(existing, requested):
        """Merge two usage types for the same physical location.

        pickup + delivery → both, delivery + pickup → both,
        both + anything → both, same + same → same.
        Never shrinks: a type already used is never taken away.
        """
        if existing not in ("pickup", "delivery", "both"):
            existing = "pickup"
        if requested not in ("pickup", "delivery", "both"):
            requested = existing
        if existing == requested:
            return existing
        return "both"

    # ── Operating hours sync ──────────────────────────────────────────

    @staticmethod
    def _parse_time_float(value):
        """'08:30' → 8.5; '17:00' → 17.0. ''/None → None."""
        if value in (None, ""):
            return None
        try:
            parts = str(value).strip().split(":")
            return int(parts[0]) + int(parts[1]) / 60.0
        except (ValueError, IndexError):
            return None

    def sync_portal_hours(self, hour_rows, scope="general"):
        """Persist a complete weekly schedule from the portal form.

        hour_rows: list of dicts, one per day (0=Monday … 6=Sunday):
            {"day": int, "status": "open_24h"|"custom"|"closed",
             "open": "HH:MM" or float, "close": "HH:MM" or float}
        Replaces the existing rows for that scope with the submitted
        state — the portal form submits the WHOLE week, so stale rows are
        archived, never left behind. Structured weekly records are the
        scheduling authority (ItineraryPlanner reads them, not the
        free-text opening_hours Char).
        """
        self.ensure_one()
        Hours = self.env["logistics.saved.location.hours"]
        old = Hours.search([
            ("saved_location_id", "=", self.id),
            ("service_scope", "=", scope),
            ("active", "=", True),
        ])
        if old:
            old.write({"active": False})
        for row in hour_rows or []:
            status = (row.get("status") or "open_24h").strip()
            if status not in ("closed", "open_24h", "custom"):
                status = "open_24h"
            open_t = self._parse_time_float(row.get("open")) if status == "custom" else 0.0
            close_t = (
                self._parse_time_float(row.get("close")) if status == "custom" else 24.0
            )
            if status == "closed":
                open_t, close_t = 0.0, 0.0
            Hours.create({
                "saved_location_id": self.id,
                "day_of_week": str(row.get("day", 0) % 7),
                "service_scope": scope,
                "status": status,
                "open_time": open_t or 0.0,
                "close_time": close_t or 24.0,
                "sequence": 10,
                "active": True,
            })
        configured = bool(Hours.search_count([
            ("saved_location_id", "=", self.id), ("active", "=", True),
        ]))
        if self.hours_status != ("configured" if configured else "not_configured"):
            self.write({
                "hours_status": "configured" if configured else "not_configured",
            })
        return True

    @api.depends("hours_ids", "hours_status")
    def _compute_hours_summary(self):
        """Compact weekly-hours label from the structured general-scope
        rows: 'Open 24h' / 'Mon–Fri 08:00–17:00' / 'Closed' / 'See hours'."""
        Hours = self.env["logistics.saved.location.hours"]
        for rec in self:
            rows = Hours.search([
                ("saved_location_id", "=", rec.id),
                ("service_scope", "=", "general"),
                ("active", "=", True),
            ])
            if not rows:
                rec.hours_summary = "Hours not set"
                continue
            by_day = {}
            for row in rows:
                by_day.setdefault(row.day_of_week, row)
            if len(by_day) == 7:
                statuses = {r.status for r in by_day.values()}
                if statuses == {"open_24h"}:
                    rec.hours_summary = "Open 24h"
                    continue
                if statuses == {"closed"}:
                    rec.hours_summary = "Closed"
                    continue

            def _label(row):
                if row.status == "open_24h":
                    return "24h"
                if row.status == "closed":
                    return "Closed"
                return "%s–%s" % (
                    self._float_to_hhmm(row.open_time or 0.0),
                    self._float_to_hhmm(row.close_time or 24.0),
                )

            # Weekday block vs weekend block, when each block is uniform.
            weekday_rows = [by_day.get(str(d)) for d in range(5) if str(d) in by_day]
            weekend_rows = [by_day.get(str(d)) for d in (5, 6) if str(d) in by_day]
            parts = []
            if len(weekday_rows) == 5 and len({_label(r) for r in weekday_rows}) == 1:
                parts.append("Mon–Fri %s" % _label(weekday_rows[0]))
            if len(weekend_rows) == 2 and len({_label(r) for r in weekend_rows}) == 1:
                parts.append("Sat–Sun %s" % _label(weekend_rows[0]))
            if len(parts) == 2:
                rec.hours_summary = " · ".join(parts)
            elif len(parts) == 1:
                rec.hours_summary = parts[0]
            else:
                rec.hours_summary = "See hours"

    def hours_context_dict(self):
        """Template context for the portal hours form: per-day status and
        HH:MM open/close strings, plus the stored timezone. Loads existing
        values back so Add/Edit always show the persisted schedule."""
        Hours = self.env["logistics.saved.location.hours"]
        days = {}
        for day in range(7):
            rows = Hours.search([
                ("saved_location_id", "=", self.id),
                ("day_of_week", "=", str(day)),
                ("service_scope", "=", "general"),
                ("active", "=", True),
            ], order="sequence", limit=1)
            if rows:
                row = rows[0]
                if row.status == "open_24h":
                    days[day] = {"status": "open_24h", "open": "00:00", "close": "23:59"}
                elif row.status == "closed":
                    days[day] = {"status": "closed", "open": "08:00", "close": "17:00"}
                else:
                    days[day] = {
                        "status": "custom",
                        "open": self._float_to_hhmm(row.open_time or 0.0),
                        "close": self._float_to_hhmm(row.close_time or 24.0),
                    }
            else:
                # Portal form default: weekdays open_24h, weekends closed.
                days[day] = {
                    "status": "open_24h" if day < 5 else "closed",
                    "open": "08:00", "close": "17:00",
                }
        return {"hours_by_day": days, "timezone": self.timezone or "America/Toronto"}

    @staticmethod
    def _float_to_hhmm(value):
        value = float(value or 0.0) % 24.0
        return "%02d:%02d" % (int(value), int(round((value % 1) * 60)) % 60)

    # ── Dispatch location sync ────────────────────────────────────────

    def _sync_dispatch_location(self):
        """Create or update the internal prema.dispatch.location mirror.

        Only writes fields that exist on prema.dispatch.location.
        Customer-facing saved location is the commercial authority;
        dispatch location is the execution mirror.

        When dispatch_location_id is already set (shared master facility),
        never touch the master's partner_id or identity data — one
        customer's saved location must not overwrite a facility shared by
        many customers. The customer↔facility relationship (portal
        visibility, pickup/delivery capability, defaults, alias, private
        contact and instructions) lives on
        logistics.location.customer.access instead.
        """
        DispatchLocation = self.env["prema.dispatch.location"].sudo()
        Access = self.env["logistics.location.customer.access"].sudo()
        for rec in self:
            # Shared master facility: keep the customer's per-facility data
            # on the ACCESS relation — never on the master itself.
            if rec.dispatch_location_id:
                master = rec.dispatch_location_id
                # Master Facility is the physical authority: keep the
                # customer copy's coordinates in sync automatically. A
                # master holding placeholder pins (0.0/0.0 — e.g. created
                # before Google verification) is healed from this copy's
                # Google-verified pair; manual pins are never clobbered.
                if not _valid_coordinate_pair(master.pin_lat, master.pin_lng):
                    if _valid_coordinate_pair(rec.latitude, rec.longitude):
                        try:
                            master.write({
                                "pin_lat": rec.latitude,
                                "pin_lng": rec.longitude,
                                "google_place_id": rec.google_place_id
                                or master.google_place_id or "",
                                "google_verified": bool(rec.google_place_id)
                                or master.google_verified,
                            })
                        except Exception:
                            _logger.warning(
                                "Could not heal master %s pins from saved "
                                "location %s", master.id, rec.id, exc_info=True,
                            )
                Access.ensure_access(
                    master, rec.commercial_partner_id,
                    portal_enabled=True,
                    can_pickup=rec._is_pickup_capable(),
                    can_delivery=rec._is_delivery_capable(),
                    is_default_pickup=rec.is_default_pickup,
                    is_default_delivery=rec.is_default_delivery,
                    customer_alias=rec.name,
                    contact_name=rec.contact_name,
                    contact_phone=rec.contact_phone,
                    contact_email=rec.contact_email,
                    pickup_instructions=rec.pickup_instructions,
                    delivery_instructions=rec.delivery_instructions,
                )
                # Master Facility is the physical authority: keep the
                # customer copy's coordinates in sync automatically.
                rec._sync_physical_from_master()
                continue

            # Build address string for dispatch location (required field)
            address_parts = [rec.street or "", rec.street2 or "", rec.city or ""]
            address = ", ".join(p for p in address_parts if p) or rec.name

            # Map customer-facing location_type → internal stop_type
            # pickup → pickup, delivery → delivery, both → both
            mapped_stop_type = rec.location_type if rec.location_type in ("pickup", "delivery", "both") else "delivery"

            # Dedupe priority (canonical facility authority): link to an
            # existing master instead of cloning it —
            #   1. same Google Place ID (the physical identity; the Google
            #      Place ID is unique per facility even when units differ)
            #   2. normalized exact address + Unit
            #   3. else create a new master (candidate for manual review)
            norm_unit = DispatchLocation._normalize_unit(rec.unit or "")
            place_id = (rec.google_place_id or "").strip()
            if place_id:
                dispatch_loc = DispatchLocation.search([
                    ("google_place_id", "=", place_id),
                    ("active", "=", True),
                ], limit=1)
            else:
                dispatch_loc = self.env["prema.dispatch.location"]
            if not dispatch_loc and (place_id or (rec.street and rec.city)):
                # Same normalization as prema.dispatch.location's stored
                # normalized_address_hash (address WITHOUT unit).
                import hashlib
                country = rec.country_id.code if rec.country_id else ""
                raw_addr = " ".join(p for p in [
                    rec.street, rec.street2, rec.city,
                    rec.state_id.code if rec.state_id else "",
                    DispatchLocation._normalize_postal(rec.postal_code or ""),
                    country,
                ] if p)
                normalized = DispatchLocation._normalize_address_street(
                    raw_addr or rec.name)
                if normalized:
                    dispatch_loc = DispatchLocation.search([
                        ("normalized_address_hash", "=",
                         hashlib.sha256(normalized.encode()).hexdigest()),
                        ("normalized_unit", "=", norm_unit),
                        ("active", "=", True),
                    ], limit=1)

            if dispatch_loc:
                # Linked to an existing master: register the customer's
                # access relation (never write onto the shared master).
                rec.dispatch_location_id = dispatch_loc.id
                # Master Facility is the physical authority, but a master
                # created before verification can hold placeholder pins
                # (0.0/0.0). When the master lacks a valid pin pair and
                # this copy carries Google-verified coordinates, push the
                # physical data to the master (manual pins are never
                # clobbered). The master's write hook then syncs copies.
                if not _valid_coordinate_pair(dispatch_loc.pin_lat, dispatch_loc.pin_lng):
                    if _valid_coordinate_pair(rec.latitude, rec.longitude):
                        try:
                            dispatch_loc.write({
                                "pin_lat": rec.latitude,
                                "pin_lng": rec.longitude,
                                "google_place_id": place_id
                                or dispatch_loc.google_place_id or "",
                                "google_verified": bool(place_id)
                                or dispatch_loc.google_verified,
                            })
                        except Exception:
                            _logger.warning(
                                "Could not update master %s pins from "
                                "saved location %s",
                                dispatch_loc.id, rec.id, exc_info=True,
                            )
                Access.ensure_access(
                    dispatch_loc, rec.commercial_partner_id,
                    portal_enabled=True,
                    can_pickup=rec._is_pickup_capable(),
                    can_delivery=rec._is_delivery_capable(),
                    is_default_pickup=rec.is_default_pickup,
                    is_default_delivery=rec.is_default_delivery,
                    customer_alias=rec.name,
                    contact_name=rec.contact_name,
                    contact_phone=rec.contact_phone,
                    contact_email=rec.contact_email,
                    pickup_instructions=rec.pickup_instructions,
                    delivery_instructions=rec.delivery_instructions,
                )
                continue

            # New master facility (first claimant): partner_id is set only
            # here at creation — later customers link to the existing master
            # through the branches above and never overwrite it.
            dispatch_loc = DispatchLocation.create({
                "name": rec.name,
                "address": address,
                "street": rec.street or "",
                "street2": rec.street2 or "",
                "unit": rec.unit or "",
                "city": rec.city or "",
                "province_code": rec.state_id.code if rec.state_id else "",
                "country_id": rec.country_id.id if rec.country_id else None,
                "pin_lat": rec.latitude or 0.0,
                "pin_lng": rec.longitude or 0.0,
                "google_place_id": rec.google_place_id or "",
                "google_verified": bool(rec.google_verified
                                        and _valid_coordinate_pair(rec.latitude,
                                                                   rec.longitude)),
                "partner_id": rec.commercial_partner_id.id,
                "business_name": rec.business_name or rec.company_name or "",
                "chain_name": rec.chain_name or "",
                "branch_name": rec.branch_name or "",
                "location_number": rec.store_number or "",
                "postal_code": rec.postal_code or "",
                "stop_type": mapped_stop_type,
            })
            rec.dispatch_location_id = dispatch_loc.id
            # First customer's access relation (same fields as the shared
            # master branch above).
            Access.ensure_access(
                dispatch_loc, rec.commercial_partner_id,
                portal_enabled=True,
                can_pickup=rec._is_pickup_capable(),
                can_delivery=rec._is_delivery_capable(),
                is_default_pickup=rec.is_default_pickup,
                is_default_delivery=rec.is_default_delivery,
                customer_alias=rec.name,
                contact_name=rec.contact_name,
                contact_phone=rec.contact_phone,
                contact_email=rec.contact_email,
                pickup_instructions=rec.pickup_instructions,
                delivery_instructions=rec.delivery_instructions,
            )

    # ── Mark used ─────────────────────────────────────────────────────

    def mark_used(self):
        """Update last_used_date when location is selected in a booking."""
        self.write({"last_used_date": fields.Datetime.now()})


class PremaDispatchLocationPhysicalSync(models.Model):
    """Keep linked customer Saved Location copies in sync with the
    canonical Master Facility's physical data.

    Lives in this module (which depends on prema_dispatch) so the sync
    can reach logistics.saved.location. Only shared PHYSICAL data is
    propagated — customer-private fields (contacts, instructions, hours,
    aliases) live on logistics.location.customer.access and are never
    touched here.
    """

    _inherit = "prema.dispatch.location"

    _PHYSICAL_SYNC_FIELDS = (
        "pin_lat", "pin_lng", "google_place_id", "google_verified",
    )

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._sync_linked_saved_locations()
        return records

    def write(self, vals):
        result = super().write(vals)
        if set(self._PHYSICAL_SYNC_FIELDS).intersection(vals):
            self._sync_linked_saved_locations()
        return result

    def _sync_linked_saved_locations(self):
        for master in self:
            if not _valid_coordinate_pair(master.pin_lat, master.pin_lng):
                continue
            copies = self.env["logistics.saved.location"].sudo().search([
                ("dispatch_location_id", "=", master.id),
                ("active", "=", True),
            ])
            copies._sync_physical_from_master()

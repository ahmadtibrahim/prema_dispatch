"""Customer Saved Location — commercial-layer address book.

Each location belongs to a commercial_partner_id. Portal users see only
their own company's locations. Internal dispatch.location records are
created/updated as execution-layer mirrors.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

_logger = logging.getLogger(__name__)


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

        # Process locations with usable addresses but not Google-verified
        domain = [
            ("google_verified", "=", False),
            ("street", "!=", False), ("street", "!=", ""),
            ("city", "!=", False), ("city", "!=", ""),
            ("active", "=", True),
        ]
        total = self.search_count(domain)
        if total == 0:
            return {
                "type": "ir.actions.client", "tag": "display_notification",
                "params": {"type": "info", "message": "All locations already verified."},
            }

        verified = 0
        names_resolved = 0
        already_ok = 0
        could_not = 0
        duplicates = 0
        report_lines = []

        batch = self.search(domain, limit=100)
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
            "view_mode": "tree,form",
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

    # ── Dispatch location sync ────────────────────────────────────────

    def _sync_dispatch_location(self):
        """Create or update the internal prema.dispatch.location mirror.

        Only writes fields that exist on prema.dispatch.location.
        Customer-facing saved location is the commercial authority;
        dispatch location is the execution mirror.

        When dispatch_location_id is already set (shared master facility),
        only update non-identity operational fields — do not overwrite
        the shared facility's core data.
        """
        DispatchLocation = self.env["prema.dispatch.location"].sudo()
        for rec in self:
            # If already linked to a shared master facility, only update
            # operational fields — don't overwrite master identity data.
            # Do NOT overwrite the global master stop_type based on one
            # customer's preference.
            if rec.dispatch_location_id:
                # Only update partner link on the master — never overwrite
                # master physical facility data with customer-specific values.
                # Customer contact/instructions stay on logistics.saved.location.
                rec.dispatch_location_id.write({
                    "partner_id": rec.commercial_partner_id.id,
                })
                continue

            # Build address string for dispatch location (required field)
            # Build address string for dispatch location (required field)
            address_parts = [rec.street or "", rec.street2 or "", rec.city or ""]
            address = ", ".join(p for p in address_parts if p) or rec.name

            # Map customer-facing location_type → internal stop_type
            # pickup → pickup, delivery → delivery, both → both
            mapped_stop_type = rec.location_type if rec.location_type in ("pickup", "delivery", "both") else "delivery"

            vals = {
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
                "partner_id": rec.commercial_partner_id.id,
                "business_name": rec.business_name or rec.company_name or "",
                "chain_name": rec.chain_name or "",
                "branch_name": rec.branch_name or "",
                "location_number": rec.store_number or "",
                "postal_code": rec.postal_code or "",
                "stop_type": mapped_stop_type,
            }
            if rec.dispatch_location_id:
                rec.dispatch_location_id.write(vals)
            else:
                dispatch_loc = DispatchLocation.create(vals)
                rec.dispatch_location_id = dispatch_loc.id

    # ── Mark used ─────────────────────────────────────────────────────

    def mark_used(self):
        """Update last_used_date when location is selected in a booking."""
        self.write({"last_used_date": fields.Datetime.now()})

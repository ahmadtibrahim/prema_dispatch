"""SAVED LOCATION CONSOLIDATION (§5–§6) — THE canonical facility resolver.

Every path that finds, reuses, or creates a facility (portal saved-location
add/edit, phone booking, future integrations) MUST go through
LocationResolverService. It owns:

  §5  resolve_or_create() — the ONE dedupe priority:
        1. normalized_google_place_key (place id + unit)
        2. normalized_address_key (full normalized address + unit)
        3. caller-supplied facility id (master reuse)
        4. CREATE
      Business name alone is NEVER a match key. On any match the existing
      facility is REUSED — the server never raises "Location already
      exists"; it returns the canonical facility and the caller then
      creates/updates the customer's access relationship.

  §6  get_or_create_access() — exactly ONE active row per
      (facility_id, commercial_partner_id). The DB unique index
      logistics_location_customer_access_unique_facility_partner already
      enforces one row per pair; this wrapper additionally resurrects a
      customer-archived row (active=True) so "pickup, then delivery"
      always lands on the SAME row with can_pickup + can_delivery both
      True — never a second row, never a hidden one.

The unique partial indexes on the two keys (prema_dispatch 18.0.3.9.0)
make a duplicate facility insert physically impossible, so reuse is the
only outcome of a re-add.
"""

class LocationResolverService:
    """Stateless per-request resolver; instantiate with request.env."""

    def __init__(self, env):
        self.env = env
        self.DispatchLoc = env["prema.dispatch.location"].sudo()
        self.Access = env["logistics.location.customer.access"].sudo()

    # ── §5 canonical facility resolution ────────────────────────────────

    def resolve_or_create(self, gv, unit="", vals=None):
        """Find-or-create the canonical facility.

        gv is the google-verify payload: either {"source": "master",
        "master": facility} (existing facility with valid pins — reused
        as-is) or {"source": "google", place_id/street/city/
        province_code/postal_code/country_code/latitude/longitude/
        formatted_address, ...} (server-verified). unit is the raw unit
        string; vals the customer-supplied facility fields.

        Returns (facility, created_bool).
        """
        vals = vals or {}
        if gv.get("source") == "master":
            return gv["master"], False

        place_id = (gv.get("place_id") or "").strip()
        norm_unit = self.DispatchLoc._normalize_unit(unit or "")
        facility = self.DispatchLoc.browse()

        # Priority 1 — Google-verified identity + unit. Keyed on the
        # stored normalized_google_place_key so the lookup hits the
        # unique partial index.
        if place_id:
            facility = self.DispatchLoc.search([
                ("normalized_google_place_key", "=",
                 self.DispatchLoc._normalize_google_place_key(place_id, norm_unit)),
                ("active", "=", True),
            ], limit=1)

        # Priority 2 — full normalized address + unit. Built from the
        # same five normalized components the stored key is computed
        # from, so an EXACT re-add always collides on the key.
        if not facility and (place_id or (gv.get("street") and gv.get("city"))):
            raw_addr = " ".join(p for p in [
                gv.get("street") or "", gv.get("city") or "",
                gv.get("province_code") or "",
                self.DispatchLoc._normalize_postal(gv.get("postal_code") or ""),
                gv.get("country_code") or "",
            ] if p)
            normalized = self.DispatchLoc._normalize_address_street(raw_addr)
            if normalized:
                facility = self.DispatchLoc.search([
                    ("normalized_address_key", "=",
                     self.DispatchLoc._normalize_address_key(normalized, norm_unit)),
                    ("active", "=", True),
                ], limit=1)

        # Priority 3 — caller-supplied canonical id (phone-booking master
        # reuse, portal hidden-field replay). Never business name alone.
        if not facility and vals.get("facility_id"):
            facility = self.DispatchLoc.browse(vals["facility_id"])
            if not facility.exists():
                facility = self.DispatchLoc.browse()

        if facility:
            return facility, False
        return self._create_facility(gv, unit, vals), True

    def _create_facility(self, gv, unit, vals):
        """Create the canonical facility from the verified Google payload
        (mirrors the portal's create contract)."""
        state_id, country_id = self._google_state_country(
            gv.get("province_code"), gv.get("country_code"))
        return self.DispatchLoc.create({
            "name": vals.get("name") or gv.get("street") or "",
            "address": gv.get("formatted_address") or ", ".join(p for p in [
                gv.get("street") or "", gv.get("city") or "",
            ] if p) or vals.get("name") or "",
            "street": gv.get("street") or vals.get("street") or "",
            "street2": vals.get("street2") or "",
            "unit": unit or "",
            "city": gv.get("city") or vals.get("city") or "",
            "province_code": gv.get("province_code") or "",
            "country_id": country_id or vals.get("country_id") or False,
            "pin_lat": gv.get("latitude") or 0.0,
            "pin_lng": gv.get("longitude") or 0.0,
            "google_place_id": (gv.get("place_id") or "").strip(),
            "google_verified": bool((gv.get("place_id") or "").strip()),
            "partner_id": vals.get("partner_id") or False,
            "business_name": vals.get("business_name") or "",
            "chain_name": vals.get("chain_name") or "",
            "branch_name": vals.get("branch_name") or "",
            "location_number": vals.get("store_number") or "",
            "postal_code": gv.get("postal_code") or vals.get("postal_code") or "",
            "stop_type": vals.get("stop_type") or "delivery",
            "dock_door": vals.get("dock_info") or "",
            "liftgate_required": bool(vals.get("liftgate_required")),
        })

    # ── §6 access get-or-create ────────────────────────────────────────

    def get_or_create_access(self, facility, partner, **vals):
        """Exactly ONE active access row per (facility, commercial partner).

        Reuses ensure_access (DB-unique per pair, archived rows found and
        resurrected) and OR-merges the capability flags: a customer who
        adds the same location as pickup and later re-adds it as delivery
        keeps BOTH capabilities on the SAME row (§6) — a re-add must
        never silently revoke the earlier capability.
        """
        access = self.Access.ensure_access(facility, partner)
        if access:
            for cap in ("can_pickup", "can_delivery"):
                vals[cap] = bool(vals.get(cap) or access[cap])
        access.write(vals)
        if not access.active:
            access.write({"active": True})
        return access

    # ── helpers ────────────────────────────────────────────────────────

    def _google_state_country(self, province_code, country_code):
        """Province/state + country codes → their res records.
        (Province_state_id, country_id) for the canonical create."""
        state_id, country_id = False, False
        if province_code:
            state = self.env["res.country.state"].sudo().search([
                ("code", "=ilike", province_code),
            ], limit=1)
            state_id = state.id if state else False
        if country_code:
            country = self.env["res.country"].sudo().search([
                ("code", "=ilike", country_code),
            ], limit=1)
            country_id = country.id if country else False
        return state_id, country_id

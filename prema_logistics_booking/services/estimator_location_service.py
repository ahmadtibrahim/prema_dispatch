"""Estimator saved-location matching (MP2 text-first workflow).

The Prema Estimator extracts addresses from freeform customer text. This
service decides, for each extracted stop, whether an existing Saved
Location (prema.dispatch.location) matches — and when the address is
complete but unmatched, creates ONE Pending Review location for it.

Contracts (MP2 acceptance):
  * match primarily on normalized street number+street, city, province and
    postal code — never on company name alone;
  * a company-name difference must not create a duplicate when the
    physical address matches;
  * created locations are verification_state=pending_review — never
    verified, never google_verified, never communicated about;
  * city-only / materially incomplete addresses are never saved;
  * repeated processing of the same address reuses the same row.
"""
import logging
import re

from odoo import fields

_logger = logging.getLogger(__name__)

_PROVINCES = {
    "ontario": "ON", "on": "ON", "quebec": "QC", "québec": "QC",
    "qc": "QC", "british columbia": "BC", "bc": "BC",
    "alberta": "AB", "ab": "AB", "manitoba": "MB", "mb": "MB",
    "saskatchewan": "SK", "sk": "SK", "nova scotia": "NS", "ns": "NS",
    "new brunswick": "NB", "nb": "NB", "newfoundland": "NL", "nl": "NL",
    "prince edward island": "PE", "pe": "PE",
}
_PROV_RE = re.compile(
    r"\b(ontario|quebec|québec|british columbia|alberta|manitoba|"
    r"saskatchewan|nova scotia|new brunswick|newfoundland|"
    r"prince edward island|on|qc|bc|ab|mb|sk|ns|nb|nl|pe)\b",
    re.IGNORECASE)


class EstimatorLocationService:
    """Match / create Saved Locations for estimator-extracted stops."""

    def __init__(self, env):
        self.env = env

    # ── Public entry ─────────────────────────────────────────────────

    def match_or_create(self, company_name, address, city="",
                        province="", postal_code="", lat=0.0, lng=0.0,
                        place_id=""):
        """Return (location_record|empty, status).

        status: saved_reused | new_pending | incomplete
        """
        Loc = self.env["prema.dispatch.location"].sudo()
        normalized = self._normalize(address, city, province, postal_code)

        existing = self._find_existing(normalized, place_id)
        if existing:
            return existing, "saved_reused"

        if not self._complete(normalized):
            return Loc, "incomplete"

        values = {
            "name": (company_name or "").strip()[:120]
                    or (address or "").strip()[:120]
                    or "Estimator location",
            "business_name": (company_name or "").strip()[:120] or False,
            "address": (address or "").strip()[:400],
            "street": (address or "").strip()[:200],
            "city": normalized["city"] or False,
            "province_code": normalized["province"] or False,
            "postal_code": normalized["postal"] or False,
            "verification_state": "pending_review",
            "source_type": "dispatcher_manual",
            "google_verified": False,
            "is_demo": False,
            "google_place_id": place_id or False,
            "pin_lat": float(lat) or 0.0,
            "pin_lng": float(lng) or 0.0,
            "pin_source": "geocoded_address" if (lat and lng) else False,
        }
        try:
            rec = Loc.create(values)
            return rec, "new_pending"
        except Exception:
            _logger.exception("estimator saved-location create failed")
            return Loc, "incomplete"

    def search(self, term, limit=8):
        """Free-text Saved Location search for the panel combobox."""
        Loc = self.env["prema.dispatch.location"].sudo()
        rows = Loc.search(
            [("location_search_key", "ilike", term.strip())],
            limit=limit)
        return [{
            "id": r.id, "name": r.name, "address": r.address or "",
            "city": r.city or "", "province_code": r.province_code or "",
            "postal_code": r.postal_code or "",
            "verification_state": r.verification_state or "",
        } for r in rows]

    # ── Normalization ────────────────────────────────────────────────

    def _normalize(self, address, city, province, postal_code):
        Loc = self.env["prema.dispatch.location"].browse()
        address = (address or "").strip()
        city = (city or "").strip()
        province = (province or "").strip()
        postal = Loc._normalize_postal(postal_code or "")

        # Province from the address tail when not given separately
        # ("... Belleville, ON" / "Belleville, Ontario").
        if not province:
            m = _PROV_RE.search(address + " " + city)
            if m:
                province = _PROVINCES.get(m.group(1).lower(),
                                          m.group(1).upper())

        norm_address = Loc._normalize_address_street(address)
        norm_city = Loc._normalize_address_street(city)
        # Mirror the location model's own compute exactly (raw join →
        # _normalize_address_street) so the key MATCHES stored keys —
        # the compute lowercases everything, so no manual casing here.
        norm_full = Loc._normalize_address_street(" ".join(
            p for p in (address, city, province, postal) if p))
        key = Loc._normalize_address_key(norm_full)
        import hashlib
        return {
            "address": norm_address, "city": norm_city,
            "province": (province or "").upper(),
            "postal": postal,
            "key": key,
            "key_ca": Loc._normalize_address_key(
                norm_full + " canada") if norm_full else "",
            "hash": hashlib.sha256(norm_full.encode()).hexdigest()
            if norm_full else "",
            "street": norm_address,
        }

    def _complete(self, norm):
        """A saved-worthy address needs a street + (city+province) or a
        street + postal. City-only or name-only inputs are incomplete."""
        if not norm["address"]:
            return False
        if norm["postal"] and len(norm["postal"]) >= 6:
            return True
        if norm["city"] and norm["province"]:
            return True
        return False

    # ── Matching ─────────────────────────────────────────────────────

    def _find_existing(self, norm, place_id):
        Loc = self.env["prema.dispatch.location"].sudo()
        if not norm["key"]:
            return Loc
        # 1. Google Place identity (highest-priority dedupe key).
        if place_id:
            hit = Loc.search([("google_place_id", "=", place_id)], limit=1)
            if hit:
                return hit
        # 2. Exact normalized address key (unit-less street+city+province
        #    + postal) — the primary MP2 match. Stored keys may or may
        #    not carry the country tail; try both shapes.
        for key in (norm["key"], norm["key_ca"]):
            if not key:
                continue
            hit = Loc.search([("normalized_address_key", "=", key)],
                             limit=1)
            if hit:
                return hit
        # 3. Hash fallback (same normalized street/city/province/postal).
        if norm["hash"]:
            hit = Loc.search([("normalized_address_hash", "=",
                               norm["hash"])], limit=1)
            if hit:
                return hit
        # 4. Postal + street-number + street name when the stored row's
        #    key predates the current normalizer.
        if norm["postal"] and len(norm["postal"]) >= 6 and norm["address"]:
            street_token = norm["address"].split()[:2]
            domain = [("postal_code", "=", norm["postal"])]
            for tok in street_token:
                domain.append(("normalized_address", "ilike",
                               "%" + tok + "%"))
            hit = Loc.search(domain, limit=1)
            if hit:
                return hit
        # 5. Street-only input ("689 Salem Rd N" with no city/province/
        #    postal): match a saved location whose normalized address
        #    STARTS with the full given street — gated on a street
        #    number so a bare "Main St" never sweeps up a random row.
        if norm["street"] and not (norm["city"] or norm["province"]
                                   or norm["postal"]):
            tokens = norm["street"].split()
            if tokens and re.match(r"^\d+[a-z]?$", tokens[0]):
                hit = Loc.search([
                    ("normalized_address", "=like",
                     norm["street"] + "%"),
                ], limit=1)
                if hit:
                    return hit
        return Loc

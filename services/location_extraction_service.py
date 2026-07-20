import base64, hashlib, json, re
from odoo.exceptions import UserError

ALLOWED_KEYS = {"success", "extraction_context", "chain_name", "location_number", "business_name", "attention", "street", "unit", "city", "province_code", "postal_code", "country_code", "full_address", "phone", "dock_door", "source_label_detected", "field_confidence", "warnings", "raw_text"}

class LocationExtractionService:
    def __init__(self, env):
        self.env = env

    def checksum(self, image_bytes, extraction_context):
        return hashlib.sha256(image_bytes + (extraction_context or "").encode()).hexdigest()

    def validate_payload(self, payload, extraction_context):
        if not isinstance(payload, dict) or not payload.get("success"):
            raise UserError("invalid_extraction_response")
        unknown = set(payload) - ALLOWED_KEYS
        if unknown:
            raise UserError("invalid_extraction_response")
        if payload.get("extraction_context") != extraction_context:
            raise UserError("invalid_extraction_response")
        confidence = payload.get("field_confidence") or {}
        if not isinstance(confidence, dict):
            raise UserError("invalid_extraction_response")
        return {k: payload.get(k, {} if k == "field_confidence" else [] if k == "warnings" else "") for k in ALLOWED_KEYS}

    def normalize_store_pattern(self, data):
        name = data.get("business_name") or ""
        m = re.search(r"\b(FOODLAND|METRO|SOBEYS|NO FRILLS|WALMART|COSTCO)\b\s*(?:STORE|WAREHOUSE)?\s*#?\s*([A-Z0-9-]+)", name, re.I)
        if m and not data.get("location_number"):
            data["chain_name"] = data.get("chain_name") or m.group(1).title()
            data["location_number"] = m.group(2).upper()
        return data

    def extract_location(self, image_bytes, extraction_context, filename=None):
        checksum = self.checksum(image_bytes, extraction_context)
        existing = self.env["prema.dispatch.location.extraction"].search([("image_checksum", "=", checksum), ("extraction_context", "=", extraction_context), ("status", "in", ("succeeded", "needs_review", "confirmed"))], limit=1)
        if existing:
            return json.loads(existing.normalized_json or existing.extracted_json or "{}")
        # Provider abstraction varies by deployment; keep a single integration seam and never hardcode credentials.
        engine = self.env["premafirm.ai.engine"] if "premafirm.ai.engine" in self.env else None
        if not engine:
            raise UserError("extraction_failed")
        raise UserError("extraction_failed")

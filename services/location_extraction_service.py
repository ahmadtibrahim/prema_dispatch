import base64
import hashlib
import json
import logging
import re

from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

ALLOWED_KEYS = {
    "success", "extraction_context", "chain_name", "location_number", "business_name",
    "attention", "street", "unit", "city", "province_code", "postal_code", "country_code",
    "full_address", "phone", "dock_door", "source_label_detected", "field_confidence",
    "warnings", "raw_text",
}

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


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
        return {
            k: payload.get(k, {} if k == "field_confidence" else [] if k == "warnings" else "")
            for k in ALLOWED_KEYS
        }

    def normalize_store_pattern(self, data):
        name = data.get("business_name") or ""
        m = re.search(
            r"\b(FOODLAND|METRO|SOBEYS|NO FRILLS|WALMART|COSTCO)\b\s*(?:STORE|WAREHOUSE)?\s*#?\s*([A-Z0-9-]+)",
            name, re.I,
        )
        if m and not data.get("location_number"):
            data["chain_name"] = data.get("chain_name") or m.group(1).title()
            data["location_number"] = m.group(2).upper()
        return data

    def _extract_json_from_text(self, raw_content):
        if not raw_content:
            return {}
        fence = _JSON_FENCE_RE.search(raw_content)
        candidate = fence.group(1).strip() if fence else raw_content.strip()
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = candidate[start:end + 1]
        try:
            return json.loads(candidate)
        except Exception:
            _logger.warning("Location extraction: could not parse AI JSON response: %s", raw_content[:300])
            return {}

    def _call_vision(self, image_bytes, mimetype, extraction_context):
        from odoo.addons.premafirm_ai_engine.services.openai_utils import openai_chat, DEFAULT_MODEL

        ICP = self.env["ir.config_parameter"].sudo()
        api_key = ICP.get_param("openai.api_key") or ICP.get_param("prema_ai.api_key")
        if not api_key:
            raise UserError("extraction_not_configured")
        model = ICP.get_param("prema_ai.fast_model") or DEFAULT_MODEL

        if extraction_context == "ship_to":
            focus = (
                "This document may show BOTH an 'Invoice To' / 'Bill To' block AND a separate "
                "'Ship To' / 'Deliver To' block. You must extract ONLY the Ship To / Deliver To "
                "block — the physical delivery destination. Completely IGNORE the Invoice To / "
                "Bill To block even if it appears first or is more prominent. If there is only one "
                "address on the document and it is not explicitly labelled 'Invoice To'/'Bill To', "
                "treat it as the Ship To address."
            )
        else:
            focus = "Extract the pickup / shipper origin address block from this document."

        system_prompt = (
            "You are a logistics document reader. " + focus + " Return ONLY a single JSON object "
            "(no prose, no markdown fences) with exactly these keys: success (boolean), "
            "extraction_context (string, echo back what was requested), chain_name (retail chain/"
            "brand name if identifiable, else empty string), location_number (printed store/location "
            "number if explicitly printed, else empty string — NEVER invent one), business_name, "
            "attention (contact name if printed, else empty string), street, unit (suite/unit number, "
            "NEVER drop this if printed), city, province_code (2-letter), postal_code, country_code "
            "(2-letter, default CA), full_address, phone, dock_door, source_label_detected (the exact "
            "label text you used to decide which block was Ship To, e.g. 'SHIP TO'), "
            "field_confidence (object mapping field name to 0-1 confidence), warnings (array of "
            "strings for anything ambiguous), raw_text (the raw text of the block you extracted)."
        )

        b64 = base64.b64encode(image_bytes).decode()
        content = [
            {"type": "text", "text": "Extract the requested address block as JSON."},
            {"type": "image_url", "image_url": {"url": f"data:{mimetype};base64,{b64}", "detail": "auto"}},
        ]
        raw = openai_chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
            max_tokens=800,
            api_key=api_key,
            model=model,
            timeout=60,
        )
        return self._extract_json_from_text(raw)

    def extract_location(self, image_bytes, extraction_context, filename=None, mimetype="image/jpeg", job_id=None, stop_id=None, attachment_id=None):
        checksum = self.checksum(image_bytes, extraction_context)
        Extraction = self.env["prema.dispatch.location.extraction"].sudo()
        existing = Extraction.search([
            ("image_checksum", "=", checksum),
            ("extraction_context", "=", extraction_context),
            ("status", "in", ("succeeded", "needs_review", "confirmed")),
        ], limit=1)
        if existing:
            return json.loads(existing.normalized_json or existing.extracted_json or "{}")

        record = Extraction.create({
            "attachment_id": attachment_id, "job_id": job_id, "stop_id": stop_id,
            "extraction_context": extraction_context, "image_checksum": checksum,
            "status": "processing",
        })
        try:
            raw_payload = self._call_vision(image_bytes, mimetype, extraction_context)
            raw_payload.setdefault("extraction_context", extraction_context)
            raw_payload.setdefault("success", bool(raw_payload))
            normalized = self.validate_payload(raw_payload, extraction_context)
            normalized = self.normalize_store_pattern(normalized)
            record.write({
                "provider_name": "openai", "extracted_json": json.dumps(raw_payload),
                "normalized_json": json.dumps(normalized),
                "warnings": json.dumps(normalized.get("warnings") or []),
                "status": "needs_review",
            })
            return normalized
        except UserError as exc:
            record.write({"status": "failed", "error_code": str(exc), "error_message": str(exc)})
            raise
        except Exception as exc:
            _logger.exception("Location extraction failed")
            record.write({"status": "failed", "error_code": "extraction_failed", "error_message": str(exc)})
            raise UserError("extraction_failed")

# -*- coding: utf-8 -*-
"""Permanent COI versions and deterministic verification.

The original attachment is never altered: each insurer/broker email creates a
new version with its own private attachment, SHA-256, sender, Message-ID and
effective/expiry dates. Exactly one direct version is current; older versions
remain viewable/downloadable.

Extraction is asynchronous (cron). The existing premafirm_ai_engine local
document extractor (PDF text/OCR) and the configured DeepSeek text model are
used; there is no second AI provider. The AI never changes the stage — it
stores findings and deterministic Odoo code decides PENDING/REVIEW/VERIFIED.
"""
import hashlib
import json
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

CONFIDENCE_FIELDS = (
    "is_coi", "named_insured", "certificate_holder_name",
    "certificate_holder_address", "auto_liability_limit", "cargo_limit",
    "effective_date", "expiry_date", "policy_numbers",
)


def _norm(value):
    return re.sub(r"[^a-z0-9]+", "", (str(value) if value is not None else "").lower())


def _json_from_text(text):
    """Parse the model response without assuming a clean JSON payload."""
    if not text:
        return None
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except (TypeError, ValueError):
        return None


class OwnerOperatorCOIVersion(models.Model):
    _name = "prema.owner.operator.coi.version"
    _description = "Owner-Operator Certificate of Insurance Version"
    _order = "id desc"

    application_id = fields.Many2one(
        "prema.owner.operator.application", required=True, index=True,
        ondelete="cascade")
    company_id = fields.Many2one(related="application_id.company_id",
                                 store=True, index=True)
    document_id = fields.Many2one(
        "prema.owner.operator.document", string="COI checklist row",
        ondelete="restrict")
    attachment_id = fields.Many2one(
        "ir.attachment", string="Original file", ondelete="restrict")
    file_name = fields.Char(related="attachment_id.name")
    mimetype = fields.Char(related="attachment_id.mimetype")
    can_preview = fields.Boolean(compute="_compute_can_preview")
    sha256 = fields.Char(string="SHA-256", index=True)
    sender = fields.Char()
    received_date = fields.Datetime()
    message_id = fields.Char(index=True)
    verification_id = fields.Many2one(
        "prema.owner.operator.verification", string="Source email",
        ondelete="set null")
    source = fields.Selection([
        ("insurer", "Insurer / broker (direct)"),
        ("applicant_forwarded", "Applicant-forwarded copy"),
        ("unknown", "Unknown / untrusted sender"),
        ("manual", "Manually attached by reviewer"),
    ], default="insurer", required=True)
    is_direct = fields.Boolean(string="Insurer-direct")
    is_current = fields.Boolean(string="Current COI", index=True)

    effective_date = fields.Date()
    expiry_date = fields.Date()
    insurer_name = fields.Char()
    broker_name = fields.Char()
    policy_number = fields.Char()
    auto_liability_limit = fields.Float()
    cargo_limit = fields.Float()
    other_coverage = fields.Text()

    extraction_state = fields.Selection([
        ("pending", "Pending"),
        ("processing", "Processing"),
        ("done", "Done"),
        ("failed", "Failed"),
    ], default="pending", required=True, index=True)
    extraction_method = fields.Char()
    extraction_result = fields.Json()
    extraction_error = fields.Text()
    extraction_confidence = fields.Float()
    verification_status = fields.Selection([
        ("pending", "Pending"),
        ("review", "Review"),
        ("verified", "Verified"),
    ], default="pending", required=True)
    verified_on = fields.Datetime(readonly=True)
    verified_by = fields.Many2one("res.users", readonly=True)

    @api.depends("attachment_id")
    def _compute_can_preview(self):
        for version in self:
            version.can_preview = bool(version.attachment_id)

    # ------------------------------------------------------------------
    # creation / current version
    # ------------------------------------------------------------------
    @api.model
    def _config(self):
        icp = self.env["ir.config_parameter"].sudo()
        return {
            "min_auto": float(
                icp.get_param(
                    "prema_owner_operator.coi_min_auto_liability") or 0.0),
            "min_cargo": float(
                icp.get_param(
                    "prema_owner_operator.coi_min_cargo") or 0.0),
            "min_conf": float(
                icp.get_param(
                    "prema_owner_operator.coi_min_ai_confidence") or 0.0),
            "holder_name": icp.get_param(
                "prema_owner_operator.coi_certificate_holder_name")
            or "PremaFirm Inc.",
            "holder_address": icp.get_param(
                "prema_owner_operator.coi_certificate_holder_address")
            or "994 Westport Cres, Unit 7A, Mississauga, ON L5T 1G1, Canada",
            "trusted": icp.get_param(
                "prema_owner_operator.trusted_insurer_domains") or "",
        }

    @api.model
    def _create_version(self, application, payload, filename, mimetype,
                        sender, received_date, message_id, verification=None,
                        source="insurer", is_direct=True):
        """Store one immutable version. Returns (version, duplicate)."""
        sha = hashlib.sha256(payload or b"").hexdigest()
        existing = self.search([
            ("application_id", "=", application.id),
            ("sha256", "=", sha),
        ], limit=1)
        if existing:
            return existing, True
        doc = application.document_ids.filtered(
            lambda d: d.document_type == "coi_insurer")[:1]
        if not doc:
            doc = self.env["prema.owner.operator.document"].sudo().create({
                "application_id": application.id,
                "document_type": "coi_insurer",
                "source": "insurer",
            })
        version = self.create({
            "application_id": application.id,
            "document_id": doc.id,
            "sha256": sha,
            "sender": sender or "",
            "received_date": received_date or fields.Datetime.now(),
            "message_id": message_id or False,
            "verification_id": verification.id if verification else False,
            "source": source,
            "is_direct": is_direct,
            "extraction_state": "pending",
            "verification_status": "pending",
        })
        attachment = self.env["ir.attachment"].sudo().create({
            "name": filename or "coi",
            "raw": payload or b"",
            "mimetype": mimetype or "application/octet-stream",
            "res_model": self._name,
            "res_id": version.id,
            "public": False,
        })
        version.write({"attachment_id": attachment.id})
        if is_direct:
            self.search([
                ("application_id", "=", application.id),
                ("is_current", "=", True),
            ]).write({"is_current": False})
            version.write({"is_current": True})
            application.write({"coi_current_version_id": version.id})
            doc.sudo().write({
                "attachment_id": attachment.id,
                "status": "received",
                "source": "insurer",
                "received_on": version.received_date,
                "notes": _(
                    "COI version %(version)s received from %(sender)s "
                    "(SHA-256 %(sha)s).") % {
                        "version": version.id,
                        "sender": sender or _("insurer"),
                        "sha": sha[:16],
                    },
            })
        application._after_coi_version(version)
        return version, False

    def _make_current(self):
        self.ensure_one()
        self.search([
            ("application_id", "=", self.application_id.id),
            ("is_current", "=", True),
        ]).write({"is_current": False})
        self.write({"is_current": True})
        self.application_id.write({"coi_current_version_id": self.id})
        doc = self.document_id or self.application_id.document_ids.filtered(
            lambda d: d.document_type == "coi_insurer")[:1]
        if doc and self.attachment_id:
            doc.sudo().write({
                "attachment_id": self.attachment_id.id,
                "status": "received",
                "source": "insurer",
                "received_on": self.received_date or fields.Datetime.now(),
            })

    # ------------------------------------------------------------------
    # extraction (asynchronous)
    # ------------------------------------------------------------------
    @api.model
    def _cron_extract_coi(self):
        versions = self.search(
            [("extraction_state", "=", "pending")], limit=5, order="id")
        for version in versions:
            try:
                version._run_extraction()
            except Exception:  # noqa: BLE001 — a bad AI run must not stop the cron
                _logger.warning(
                    "COI extraction failed for version %s", version.id,
                    exc_info=False)
                version.write({
                    "extraction_state": "failed",
                    "extraction_error": _("AI extraction failed."),
                })
            version._evaluate_verification()
        return True

    def _run_extraction(self):
        self.ensure_one()
        if not self.attachment_id:
            self.write({
                "extraction_state": "failed",
                "extraction_error": _("No COI attachment."),
            })
            return False
        try:
            from odoo.addons.premafirm_ai_engine.services import (
                document_extractor)
        except ImportError:
            self.write({
                "extraction_state": "failed",
                "extraction_error": _("Document extractor unavailable."),
            })
            return False
        raw = self.attachment_id.raw or b""
        text, method = document_extractor.extract_text(
            raw, self.mimetype or "", self.file_name or "")
        if not text or method == "none":
            self.write({
                "extraction_state": "failed",
                "extraction_error": _(
                    "Unreadable document or no extractable text."),
            })
            return False
        prompt = self._extraction_prompt(text)
        try:
            from odoo.addons.premafirm_ai_engine.services.deepseek_utils import (
                deepseek_chat)
            response = deepseek_chat(
                messages=[{"role": "user", "content": prompt}],
                system=(
                    "You extract structured facts from insurance certificates. "
                    "Return ONLY valid JSON; never add prose or markdown."),
                max_tokens=2600,
                env=self.env,
            )
        except Exception:  # noqa: BLE001
            self._logger_extraction_failure()
            self.write({
                "extraction_state": "failed",
                "extraction_error": _("AI extraction failed."),
            })
            return False
        result = _json_from_text(response)
        if not result:
            self.write({
                "extraction_state": "failed",
                "extraction_error": _("AI extraction returned invalid JSON."),
            })
            return False
        self.write({
            "extraction_state": "done",
            "extraction_method": method,
            "extraction_result": result,
            "extraction_error": False,
            "extraction_confidence": self._min_confidence(result),
        })
        self._apply_extracted_fields(result)
        return True

    def _logger_extraction_failure(self):
        # Never log the document or the model's raw response.
        _logger.warning(
            "COI AI extraction failed for version %s", self.id)

    def _extraction_prompt(self, text):
        app = self.application_id
        return (
            "Extract this Certificate of Insurance. For EVERY field return an "
            "object with value, confidence (0..1), page (integer) and "
            "evidence (a short quote from the document). Use null when absent.\n"
            "JSON schema:\n"
            '{"is_coi":{"value":true,"confidence":0.0,"page":1,"evidence":""},'
            '"named_insured":{...},"insurer":{...},"broker":{...},'
            '"policy_numbers":{"value":[],"confidence":0.0,"page":1,"evidence":""},'
            '"auto_liability_limit":{...},"cargo_limit":{...},'
            '"other_coverage":{"value":[],"confidence":0.0,"page":1,"evidence":""},'
            '"effective_date":{"value":"YYYY-MM-DD",...},'
            '"expiry_date":{"value":"YYYY-MM-DD",...},'
            '"certificate_holder_name":{...},"certificate_holder_address":{...},'
            '"vehicles":{"value":[],"confidence":0.0,"page":1,"evidence":""},'
            '"contradictions":{"value":[],"confidence":0.0,"page":1,"evidence":""}}\n'
            "Limits are numbers in CAD. Dates are ISO. Do not infer a missing "
            "value. Flag contradictory policy numbers or dates in contradictions.\n"
            "Application legal business name (comparison only, not evidence): %s\n"
            "Configured certificate holder: %s — %s\n"
            "DOCUMENT TEXT:\n%s"
        ) % (
            app.business_name or "",
            self._config()["holder_name"],
            self._config()["holder_address"],
            (text or "")[:12000],
        )

    @api.model
    def _value(self, data, key):
        item = (data or {}).get(key)
        if isinstance(item, dict):
            return (item.get("value"), float(item.get("confidence") or 0.0),
                    item.get("page"), item.get("evidence") or "")
        return item, 0.0, None, ""

    @api.model
    def _min_confidence(self, data):
        values = [self._value(data, key)[1] for key in CONFIDENCE_FIELDS]
        return min(values) if values else 0.0

    def _apply_extracted_fields(self, data):
        self.ensure_one()

        def val(key):
            return self._value(data, key)[0]

        def as_money(value):
            if value is None:
                return 0.0
            if isinstance(value, (int, float)):
                return float(value)
            digits = re.sub(r"[^0-9.]", "", str(value))
            try:
                return float(digits) if digits else 0.0
            except ValueError:
                return 0.0

        def as_date(value):
            if not value:
                return False
            match = re.search(r"\d{4}-\d{2}-\d{2}", str(value))
            if match:
                return match.group(0)
            return False

        policies = val("policy_numbers")
        if isinstance(policies, list):
            policies = ", ".join(str(p) for p in policies if p)
        other = val("other_coverage")
        if isinstance(other, list):
            other = "\n".join(str(p) for p in other if p)
        self.write({
            "effective_date": as_date(val("effective_date")) or False,
            "expiry_date": as_date(val("expiry_date")) or False,
            "insurer_name": val("insurer") or False,
            "broker_name": val("broker") or False,
            "policy_number": policies or False,
            "auto_liability_limit": as_money(val("auto_liability_limit")),
            "cargo_limit": as_money(val("cargo_limit")),
            "other_coverage": other or False,
        })

    # ------------------------------------------------------------------
    # deterministic decision
    # ------------------------------------------------------------------
    def _evaluate_verification(self):
        self.ensure_one()
        app = self.application_id
        if not app or not self.is_current or not self.is_direct:
            return False
        if self.extraction_state not in ("done", "failed"):
            return False
        cfg = self._config()
        if self.extraction_state == "failed":
            app._set_verification_review(
                [self.extraction_error or _("AI extraction failed.")],
                method="ai")
            self.write({"verification_status": "review"})
            return True
        reasons = []
        if cfg["min_auto"] <= 0:
            reasons.append(_("Coverage threshold for automobile liability is not configured."))
        if cfg["min_cargo"] <= 0:
            reasons.append(_("Coverage threshold for cargo is not configured."))
        if cfg["min_conf"] <= 0:
            reasons.append(_("AI confidence threshold is not configured."))
        if reasons:
            app._set_verification_review(reasons, method="ai")
            self.write({"verification_status": "review"})
            return True

        data = self.extraction_result or {}
        min_conf = cfg["min_conf"]

        def read(key):
            return self._value(data, key)

        def conf_ok(key, label):
            value, confidence, page, evidence = read(key)
            if value in (None, "", [], {}):
                reasons.append(_("%s is missing.") % label)
                return False
            if confidence < min_conf:
                reasons.append(_("Low-confidence extraction: %s.") % label)
                return False
            return True

        is_coi, is_coi_conf = read("is_coi")[0], read("is_coi")[1]
        if not is_coi or is_coi_conf < min_conf:
            reasons.append(_("Document is not an identifiable Certificate of Insurance."))

        named, named_conf = read("named_insured")[0], read("named_insured")[1]
        if not named:
            reasons.append(_("Named insured is missing."))
        elif _norm(named) != _norm(app.business_name or ""):
            reasons.append(_("Named insured does not match the application business name."))
        if named and named_conf < min_conf:
            reasons.append(_("Low-confidence extraction: named insured."))

        holder_name, holder_name_conf = read("certificate_holder_name")[0], \
            read("certificate_holder_name")[1]
        holder_addr, holder_addr_conf = read("certificate_holder_address")[0], \
            read("certificate_holder_address")[1]
        if not holder_name or "premafirm" not in _norm(holder_name):
            reasons.append(_("Certificate holder name does not identify PremaFirm Inc."))
        elif holder_name_conf < min_conf:
            reasons.append(_("Low-confidence extraction: certificate holder name."))
        holder_norm = _norm(holder_addr)
        configured_tokens = [
            token for token in re.findall(r"[A-Za-z0-9]+", cfg["holder_address"])
            if len(token) >= 4
            and token.lower() not in ("unit", "canada", "inc", "ltd")]
        if not holder_addr or not configured_tokens or not all(
                token.lower() in holder_norm for token in configured_tokens):
            reasons.append(_("Certificate holder address does not match the configured address."))
        elif holder_addr_conf < min_conf:
            reasons.append(_("Low-confidence extraction: certificate holder address."))

        for key, label in (
                ("auto_liability_limit", _("Commercial automobile liability")),
                ("cargo_limit", _("Cargo coverage"))):
            value, confidence = read(key)[0], read(key)[1]
            minimum = cfg["min_auto"] if key == "auto_liability_limit" \
                else cfg["min_cargo"]
            amount = 0.0
            if isinstance(value, (int, float)):
                amount = float(value)
            elif value:
                digits = re.sub(r"[^0-9.]", "", str(value))
                try:
                    amount = float(digits) if digits else 0.0
                except ValueError:
                    amount = 0.0
            if not value:
                reasons.append(_("%s is missing.") % label)
            elif amount < minimum:
                reasons.append(_("%s is below the configured minimum.") % label)
            elif confidence < min_conf:
                reasons.append(_("Low-confidence extraction: %s.") % label)

        policies = read("policy_numbers")[0]
        policies = policies if isinstance(policies, list) else \
            ([policies] if policies else [])
        if not policies:
            reasons.append(_("Policy number is missing."))
        elif read("policy_numbers")[1] < min_conf:
            reasons.append(_("Low-confidence extraction: policy number."))
        if len({str(p).strip().lower() for p in policies if p}) > 1:
            reasons.append(_("Conflicting policy numbers."))

        eff_date, eff_conf = read("effective_date")[0], read("effective_date")[1]
        exp_date, exp_conf = read("expiry_date")[0], read("expiry_date")[1]
        if not eff_date:
            reasons.append(_("Policy effective date is missing."))
        if not exp_date:
            reasons.append(_("Policy expiry date is missing."))
        if eff_date or exp_date:
            today = fields.Date.context_today(self)
            try:
                eff = fields.Date.to_date(eff_date) if eff_date else False
                exp = fields.Date.to_date(exp_date) if exp_date else False
            except (TypeError, ValueError):
                eff = exp = False
                reasons.append(_("Policy dates are not valid."))
            if eff and exp and eff > exp:
                reasons.append(_("Conflicting policy dates."))
            if eff and eff > today:
                reasons.append(_("Policy is not yet effective."))
            if exp and exp < today:
                reasons.append(_("Policy is expired."))
            if eff_conf < min_conf and eff_date:
                reasons.append(_("Low-confidence extraction: effective date."))
            if exp_conf < min_conf and exp_date:
                reasons.append(_("Low-confidence extraction: expiry date."))

        contradictions = read("contradictions")[0]
        if isinstance(contradictions, list):
            contradictions = [c for c in contradictions if c]
        if contradictions:
            reasons.append(_("Contradictory information: %s.")
                           % ", ".join(str(c) for c in contradictions))

        reasons = list(dict.fromkeys(reasons))
        if reasons:
            app._set_verification_review(
                reasons, method="ai",
                confidence=self.extraction_confidence or 0.0)
            self.write({"verification_status": "review"})
        else:
            app._set_verification_verified(
                self, method="ai",
                confidence=self.extraction_confidence or 0.0)
            self.write({"verification_status": "verified"})
        return True

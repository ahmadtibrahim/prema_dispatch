# -*- coding: utf-8 -*-
"""Dispatch-side companion for the E-A2 quotation contract (MP1).

Turns a CRM opportunity's effective shipment facts (engine LeadFactService,
see docs/A2_CROSS_MODULE_CONTRACT.md in the engine repo) into the two
deliberate staff artifacts:

* a preliminary-estimate PRICE request through the canonical dispatch path
  (BookingOrchestrationService.normalize_request + prepare_quote) — the only
  place an amount may come from;
* the population payload of the lead's DRAFT Customer Rate Confirmation
  (logistics.custom.quote) — never priced, never sent, never booked here.

Location resolution deliberately mirrors the staff phone-quote wizard
(wizards/phone_booking.py, prema_dispatch commit 00b9a58 "city-only rows are
never reusable as quote stops"):

* a quote stop may reuse a Saved Location / Master Facility ONLY when the
  matched row carries a postal code (an FSA to price against);
* a complete civic address + postal code that matches nothing is saved as a
  new Pending Review facility (with customer access when a partner exists);
* city-only text ("Milton, ON") is never persisted and never reused as an
  operational stop — callers must refuse it;
* the service itself never invents an address: every stop value comes from
  the effective customer facts (newest customer email supersedes).

Hard safety guarantees: this service never sends mail, never changes a
pipeline state, never confirms and never books.  All engine imports stay
inside methods (the engine must never import dispatch code — module cycle).
"""

import datetime
import logging
import re

from odoo import _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# ── text rules (mirror of wizards/phone_booking.py) ──────────────────────
CIVIC_NUMBER_RE = re.compile(r"\b\d+[A-Za-z]?\b")
POSTAL_RE = re.compile(
    r"\b([ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTVWXYZ])\s*"
    r"(\d[ABCEGHJ-NPRSTVWXYZ]\d)\b",
    re.IGNORECASE,
)
PROVINCE_RE = re.compile(
    r"\b(AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT)\b", re.IGNORECASE)

REEFER_RE = re.compile(
    r"\b(?:frozen|chilled|reefer|refrigerated|temperature[- ]controlled)\b",
    re.IGNORECASE,
)
DRY_RE = re.compile(
    r"\b(?:dry|ambient|room[- ]?temperature)\b", re.IGNORECASE)
FTL_RE = re.compile(
    r"\b(?:ftl|full\s*truckload|full\s*load|truckload|dedicated\s*truck)\b",
    re.IGNORECASE,
)
SETPOINT_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*(?:°\s*)?(f|fahrenheit|c|celsius)?\b",
    re.IGNORECASE,
)
WEIGHT_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*(kg|kgs|kilograms|lb|lbs|pounds)?\b",
    re.IGNORECASE,
)
INT_RE = re.compile(r"\d+")


def _iso_at(at):
    """Serialize a document timestamp the way the engine does for its
    facts_snapshot sources (naive-UTC ISO, seconds precision, None → '').

    Kept at module level so replay keys, doc-identity checks and the engine
    snapshot always line up byte for byte."""
    if at is None:
        return ""
    if isinstance(at, datetime.datetime):
        if at.tzinfo is not None:
            from pytz import UTC
            at = at.astimezone(UTC).replace(tzinfo=None)
        return at.isoformat(sep=" ", timespec="seconds")
    return str(at)


class LeadQuoteDraftService:
    """CRM opportunity → draft estimate / draft Rate Confirmation helper."""

    def __init__(self, env):
        self.env = env

    # ════════════════════════════════════════════════════════════════════
    # Engine fact extraction (import only — the engine never imports us)
    # ════════════════════════════════════════════════════════════════════

    def extract_effective_facts(self, lead, extractor=None, docs=None):
        """Customer documents → superseded per-field effective facts.

        The extractor defaults to :meth:`compat_extractor` (never the raw
        engine default): the engine calls per-document extractors with
        ``source=`` while ``extract_from_text`` declares ``source_label=``,
        and this dispatch side must not depend on which spelling the engine
        uses internally (engine lead_fact_service currently TypeErrors on
        its own default).
        """
        from odoo.addons.premafirm_ai_engine.services.lead_fact_service import (  # noqa: E501
            LeadFactService,
        )
        if extractor is None:
            extractor = self.compat_extractor()
        return LeadFactService(self.env).extract_effective_facts(
            lead, extractor=extractor, docs=docs)

    def compat_extractor(self):
        """Live extractor callable tolerant of BOTH kwarg spellings.

        LeadFactService calls per-document extractors as
        ``extractor(text, source=..., kind=..., at=...)`` while
        ShipmentFactExtractionService.extract_from_text declares the same
        argument as ``source_label=``.  This wrapper accepts both and
        forwards to the declared signature, so dispatch-side callers never
        TypeError regardless of the engine's internal spelling (fixed or
        not).
        """
        def extractor(text, source=None, source_label=None,
                      kind="lead_description", at=None):
            from odoo.addons.premafirm_ai_engine.services.shipment_fact_extraction_service import (  # noqa: E501
                ShipmentFactExtractionService,
            )
            return ShipmentFactExtractionService(self.env).extract_from_text(
                text,
                source_label=source_label
                if source_label is not None else (source or ""),
                kind=kind,
                at=at,
            )
        return extractor

    def customer_documents_covered_by_draft(self, lead, draft):
        """True when every current customer document of the lead is already
        represented in the draft's stored facts snapshot — i.e. the draft is
        current and a repeat click must reopen it (never a duplicate).

        Document identity is (kind, source, at), exactly the ``sources`` the
        engine stores in ``facts_snapshot``, serialized the same way
        (naive-UTC seconds).  Using identity — not wall-clock comparison —
        keeps the guard immune to future-dated inbound mail (test fixtures
        deliberately date customer emails ahead of ``now``): any document
        the draft was built FROM is covered whatever its date says.
        """
        from odoo.addons.premafirm_ai_engine.services.lead_fact_service import (  # noqa: E501
            LeadFactService,
        )
        sources = (draft.facts_snapshot or {}).get("sources") or []
        draft_keys = {
            (str(source.get("kind") or ""), str(source.get("source") or ""),
             str(source.get("at") or ""))
            for source in sources
        }
        docs = LeadFactService(self.env).collect_documents(lead)
        current_keys = {
            (str(doc.get("kind") or ""), str(doc.get("source") or ""),
             _iso_at(doc.get("at")))
            for doc in docs
        }
        return draft_keys >= current_keys

    def replay_extractor(self, facts_result):
        """Callable that replays already-extracted rows document by document.

        The estimate draft (prepare_from_lead) runs the extractor AGAIN over
        the same documents (nothing was written in between), so replaying the
        rows we just extracted guarantees the draft's facts_snapshot is
        EXACTLY the facts that were location-resolved and priced — without a
        second AI call.  Rows are keyed by the source label + document text
        they were extracted from; an unknown document falls back to the live
        extractor (paranoia).
        """
        rows_by_key = {}
        text_by_key = {}
        for row in facts_result.get("rows") or []:
            key = (str(row.get("source") or ""), str(row.get("at") or ""),
                   str(row.get("kind") or ""))
            rows_by_key.setdefault(key, []).append(dict(row))

        for doc in facts_result.get("docs") or []:
            key = (str(doc.get("source") or ""), _iso_at(doc.get("at")),
                   str(doc.get("kind") or ""))
            text_by_key[key] = (doc.get("text") or "").strip()
            rows_by_key.setdefault(key, [])

        def extractor(text, source="", kind="lead_description", at=None):
            key = (str(source or ""), _iso_at(at), str(kind or ""))
            if key in rows_by_key and key in text_by_key \
                    and text_by_key[key] == (text or "").strip():
                return {"rows": rows_by_key[key], "warnings": []}
            from odoo.addons.premafirm_ai_engine.services.shipment_fact_extraction_service import (  # noqa: E501
                ShipmentFactExtractionService,
            )
            _logger.warning(
                "Estimate draft: no cached rows for document %r — live "
                "extraction used.", source)
            return ShipmentFactExtractionService(self.env).extract_from_text(
                text, source_label=source, kind=kind, at=at)
        return extractor

    # ════════════════════════════════════════════════════════════════════
    # Saved-location resolution (00b9a58 rules — mirror of the phone wizard)
    # ════════════════════════════════════════════════════════════════════

    @staticmethod
    def postal_from_address(address):
        """Canadian postal code embedded in a text blob ('' when none)."""
        match = POSTAL_RE.search(address or "")
        return ("%s %s" % (match.group(1), match.group(2))).upper() \
            if match else ""

    @classmethod
    def _canonical_postal(cls, value):
        """Postal value normalized to spaced uppercase ('' when invalid)."""
        return cls.postal_from_address(str(value or ""))

    def _location_model(self):
        return self.env["prema.dispatch.location"].sudo()

    def match_saved_location(self, address, postal_code):
        """Return only a confident postal-bearing facility match; create
        nothing.  City-only rows are never reusable as quote stops."""
        Location = self._location_model()
        address = (address or "").strip()
        if not address:
            return Location.browse()

        def reusable(location):
            return bool(
                (location.postal_code or "").strip()
                or self.postal_from_address(location.address))

        exact = Location.search([
            ("active", "=", True),
            ("address", "=ilike", address),
        ], limit=1).filtered(reusable)
        if exact:
            return exact
        normalized = Location._normalize_address_street(address)
        if normalized:
            match = Location.search([
                ("active", "=", True),
                ("normalized_address", "=", normalized),
            ], limit=1).filtered(reusable)
            if match:
                return match
        postal = Location._normalize_postal(postal_code or "")
        if postal:
            candidates = Location.search([
                ("active", "=", True),
                ("postal_code", "=ilike", postal),
            ], limit=20)
            postal_matches = candidates.filtered(
                lambda location: (
                    location.normalized_address == normalized
                    and reusable(location)))
            if len(postal_matches) == 1:
                return postal_matches
        return Location.browse()

    @staticmethod
    def manual_location_values(address, postal_code, company_name, stop_type):
        """Reviewable Pending Review facility row from a COMPLETE civic
        address; None when the address is not saveable."""
        address = (address or "").strip()
        postal_code = (postal_code or "").strip().upper()
        if not address or not postal_code \
                or not CIVIC_NUMBER_RE.search(address):
            return None
        parts = [part.strip() for part in address.split(",") if part.strip()]
        province_match = PROVINCE_RE.search(address)
        city = ""
        if province_match and len(parts) >= 2:
            province_index = next(
                (index for index, part in enumerate(parts)
                 if PROVINCE_RE.search(part)),
                len(parts) - 1,
            )
            if province_index > 0:
                city = parts[province_index - 1]
        street = parts[0] if parts else address
        return {
            "name": (company_name or city or address)[:80],
            "business_name": company_name or "",
            "address": address,
            "street": street,
            "city": city,
            "province_code": (province_match.group(1).upper()
                              if province_match else ""),
            "postal_code": postal_code,
            "stop_type": stop_type,
            "source_type": "dispatcher_manual",
            "verification_state": "pending_review",
        }

    def ensure_customer_location_access(self, location, stop_type, partner):
        """Get-or-create the customer's private access row for the facility
        (the canonical per-customer metadata carrier)."""
        if not location or not partner:
            return
        self.env["logistics.location.customer.access"].sudo() \
            .ensure_access(location, partner.commercial_partner_id,
                           **({"can_pickup" if stop_type == "pickup"
                               else "can_delivery": True}))

    def _location_payload(self, location, stop_type, partner):
        """Physical master + only this customer's private metadata (mirror
        of the phone wizard's payload contract for stop records)."""
        if not location:
            return {}
        access = self.env["logistics.location.customer.access"].sudo().search([
            ("facility_id", "=", location.id),
            ("active", "=", True),
            ("commercial_partner_id", "=",
             partner.commercial_partner_id.id if partner else 0),
        ], order="id desc", limit=1)
        return {
            "company_name": location.business_name or location.name or "",
            "formatted_address": (location.address_formatted
                                  or location.address or ""),
            "street": location.street or location.address or "",
            "city": location.city or "",
            "postal_code": location.postal_code or "",
            "latitude": location.pin_lat or 0.0,
            "longitude": location.pin_lng or 0.0,
            "instructions": (
                (access.pickup_instructions if access else "")
                if stop_type == "pickup"
                else (access.delivery_instructions if access else "")),
            "contact_name": (access.contact_name if access else "") or "",
            "phone": (access.contact_phone if access else "") or "",
            "email": (access.contact_email if access else "") or "",
            "dispatch_location_id": location.id,
            "facility_id": location.id,
            "customer_access_id": access.id if access else False,
        }

    # ════════════════════════════════════════════════════════════════════
    # Fact → stop resolution (the contract §6 mapping duty)
    # ════════════════════════════════════════════════════════════════════

    @staticmethod
    def _fact_value(facts, field):
        fact = ((facts or {}).get("effective") or {}).get(field)
        return str((fact or {}).get("value") or "").strip()

    @classmethod
    def _side_stop(cls, facts, side):
        """Best-effort stop text from the effective facts for one side.

        Composed from the full-address fact first, then the city/postal
        parts — only fields the customer actually stated are ever used."""
        prefix = "origin" if side == "pickup" else "destination"
        address = cls._fact_value(facts, "%s_address" % prefix)
        city = cls._fact_value(facts, "%s_city" % prefix)
        postal = cls._canonical_postal(cls._fact_value(
            facts, "%s_postal_code" % prefix))
        if not postal:
            # Customers often paste the full address as ONE string — the FSA
            # embedded in that side's own text still anchors pricing (same
            # convention the phone wizard applies to typed addresses).
            postal = cls.postal_from_address(address)
        if not address:
            # No street was stated — keep the bare "city, postal" text so
            # the review still shows exactly what the customer said.
            address = ", ".join(part for part in (city, postal) if part)
        return {
            "side": side,
            "stop_type": side,
            "address": address or "",
            "city": city or "",
            "postal_code": postal or "",
        }

    @staticmethod
    def _kind(stop):
        """complete / postal_only / civic_only / city_only / none.

        complete    — civic number AND postal code (saveable as facility);
        postal_only — postal code without civic number (priceable text);
        civic_only  — civic number without postal code (operational text,
                      not priceable);
        city_only   — neither (e.g. "Milton, ON") — NEVER operational;
        none        — nothing was stated at all."""
        address = stop["address"]
        has_civic = bool(CIVIC_NUMBER_RE.search(address))
        has_postal = bool(stop["postal_code"])
        if has_civic and has_postal:
            return "complete"
        if has_postal:
            return "postal_only"
        if has_civic:
            return "civic_only"
        return "city_only" if address else "none"

    def resolve_stops(self, lead, facts, require_priceable):
        """Resolve origin/destination against the saved-location rules.

        Validates BOTH sides before writing anything, then saves each
        unmatched COMPLETE address as a Pending Review facility (with
        customer access).  Returns {"pickup": stop, "delivery": stop,
        "created": locations}; raises with an actionable message when a side
        is city-only or (for require_priceable) not priceable — before any
        row is persisted."""
        partner = lead.partner_id
        stops = {}
        for side in ("pickup", "delivery"):
            stop = self._side_stop(facts, side)
            kind = self._kind(stop)
            location = self.match_saved_location(stop["address"],
                                                 stop["postal_code"])
            if location:
                kind = "location"
            stop["kind"] = kind
            stop["location"] = location
            stops[side] = stop

        errors = []
        for side, stop in stops.items():
            label = _("Pickup") if side == "pickup" else _("Delivery")
            if stop["kind"] == "none":
                errors.append(_(
                    "%s: no address was stated on the opportunity (check "
                    "the description and the inbound customer emails).")
                    % label)
            elif stop["kind"] == "city_only":
                errors.append(_(
                    "%s: only a city was stated (%s) — city-only locations "
                    "are never used as operational stops. State the full "
                    "civic address + postal code on the opportunity, or "
                    "quote manually via 'Calculate Dispatch Rate'.")
                    % (label, stop["address"]))
            elif require_priceable and stop["kind"] == "civic_only":
                errors.append(_(
                    "%s: %s has no postal code — pricing needs a postal "
                    "code or a verified Saved Location. Complete it on the "
                    "opportunity first.") % (label, stop["address"]))
        if errors:
            raise UserError(_(
                "The shipment stop could not be resolved from the latest "
                "customer facts:\n%s\nNothing was created or priced.")
                % "\n".join(errors))

        # ── Deliberate save: unmatched COMPLETE addresses → Pending Review ──
        # Re-match right before each create (like the wizard's per-side
        # ensure flow): an earlier side of this very call may have already
        # saved the same civic address (identical pickup/delivery), and a
        # repeat click must reuse the pending row — never duplicate it.
        created = self._location_model()
        for side, stop in stops.items():
            if stop["kind"] != "complete":
                continue
            location = stop["location"] or self.match_saved_location(
                stop["address"], stop["postal_code"])
            if location:
                stop["location"] = location
                stop["kind"] = "location"
                continue
            values = self.manual_location_values(
                stop["address"], stop["postal_code"], "", side)
            if not values:
                continue
            location = self._location_model().create(values)
            self.ensure_customer_location_access(location, side, partner)
            stop["location"] = location
            stop["kind"] = "new_pending_review"
            created |= location
            _logger.info(
                "Lead %s: %s address %r saved as Pending Review facility %s",
                lead.id, side, stop["address"], location.id)
        return {"pickup": stops["pickup"], "delivery": stops["delivery"],
                "created": created}

    def _stop_payload(self, stop, shipment, partner):
        """One side's request stop (mirror of the wizard's _stop_values)."""
        location = stop.get("location")
        payload = self._location_payload(
            location, stop["side"], partner) if location else {}
        payload.update({
            "company_name": payload.get("company_name") or "",
            "formatted_address": payload.get("formatted_address")
                or stop["address"] or "",
            "street": payload.get("street") or stop["address"] or "",
            "postal_code": payload.get("postal_code")
                or stop["postal_code"] or "",
            "instructions": payload.get("instructions") or "",
            "pallet_count": shipment["pallets"],
            "pallets": shipment["pallets"],
            "weight_lb": shipment["weight_lbs"],
            "weight_lbs": shipment["weight_lbs"],
            "liftgate_required": False,
        })
        return payload

    # ════════════════════════════════════════════════════════════════════
    # Fact → shipment values
    # ════════════════════════════════════════════════════════════════════

    @staticmethod
    def _parse_int(value):
        match = INT_RE.search(str(value or ""))
        return int(match.group(0)) if match else None

    @staticmethod
    def _parse_iso_date(value):
        try:
            return datetime.date.fromisoformat(str(value)[:10])
        except (TypeError, ValueError):
            return None

    @classmethod
    def _parse_weight_lbs(cls, value):
        match = WEIGHT_RE.search(str(value or ""))
        if not match:
            return None
        number = float(match.group(1).replace(",", ""))
        unit = (match.group(2) or "").lower()
        if unit in ("kg", "kgs", "kilograms"):
            number *= 2.20462
        return round(number, 1)

    @staticmethod
    def _setpoint_celsius(*texts):
        """First explicit numeric setpoint across texts → Celsius.

        F/Fahrenheit converts; a bare number is taken as Celsius (the
        canonical storage unit — the same convention the phone wizard
        applies to unmarked numbers).  None when no number is stated.
        """
        for text in texts:
            if not text:
                continue
            match = SETPOINT_RE.search(str(text))
            if not match:
                continue
            value = float(match.group(1))
            unit = (match.group(2) or "c").lower()
            if unit in ("f", "fahrenheit"):
                value = (value - 32.0) * 5.0 / 9.0
            return round(value, 2)
        return None

    @classmethod
    def _temperature_mode(cls, facts):
        """reefer vs dry — canonical ordering:

        1. any reefer language in temperature_mode/setpoint/commodity → reefer;
        2. else explicit dry language in temperature_mode/setpoint/equipment
           → dry;
        3. else equipment says reefer (and nothing dry contradicts it)
           → reefer;
        4. else dry (the canonical base default)."""
        mode = cls._fact_value(facts, "temperature_mode")
        setpoint = cls._fact_value(facts, "temperature_setpoint")
        equipment = cls._fact_value(facts, "equipment")
        commodity = cls._fact_value(facts, "commodity")
        if (REEFER_RE.search(mode) or REEFER_RE.search(setpoint)
                or REEFER_RE.search(commodity)):
            return "reefer", mode, setpoint, equipment
        if (DRY_RE.search(mode) or DRY_RE.search(setpoint)
                or DRY_RE.search(equipment)):
            return "dry", mode, setpoint, equipment
        if REEFER_RE.search(equipment):
            return "reefer", mode, setpoint, equipment
        return "dry", mode, setpoint, equipment

    def shipment_values(self, facts):
        """Parsed shipment values from the effective facts (only dry/ltl
        are ever defaults — the canonical base defaults)."""
        def f(field):
            return self._fact_value(facts, field)

        temperature_mode, mode_text, setpoint_text, equipment = \
            self._temperature_mode(facts)
        setpoint_c = None
        if temperature_mode == "reefer":
            setpoint_c = self._setpoint_celsius(
                setpoint_text, mode_text, equipment)
        load_type = "ftl" if FTL_RE.search("%s %s %s" % (
            f("equipment"), f("accessorials"), f("instructions"))) else "ltl"

        pallets = self._parse_int(f("pallets"))
        weight_lbs = self._parse_weight_lbs(f("weight_lbs"))
        pickup_date = self._parse_iso_date(f("pickup_date"))
        return {
            "pallets": pallets or 1,
            # Stated-ness flags: priceable actions must not silently price a
            # never-stated load size as one pallet of nothing.
            "pallets_stated": bool(pallets),
            "weight_lbs": weight_lbs if weight_lbs is not None else 0.0,
            "weight_stated": weight_lbs is not None,
            "commodity": f("commodity"),
            "load_type": load_type,
            "temperature_mode": temperature_mode,
            "required_temperature_c": setpoint_c,
            "temperature_setpoint_stated": bool(setpoint_c is not None),
            "requested_pickup_date": pickup_date,
            # Facts that belong on the review notes, not on priced fields.
            "accessorials": f("accessorials"),
            "instructions": f("instructions"),
            "contacts": f("contacts"),
            "stops": f("stops"),
            "document_number": f("document_number"),
            "service_minutes": f("service_minutes"),
            "delivery_deadline": f("delivery_deadline"),
            "pickup_earliest": f("pickup_earliest"),
            "pickup_latest": f("pickup_latest"),
        }

    # ════════════════════════════════════════════════════════════════════
    # Draft RC population payload
    # ════════════════════════════════════════════════════════════════════

    def rc_notes_lines(self, shipment):
        """Reviewer-friendly factual note lines from the customer facts."""
        lines = []
        if shipment["requested_pickup_date"]:
            lines.append("Pickup date: %s" % shipment["requested_pickup_date"])
        if shipment["pickup_earliest"] or shipment["pickup_latest"]:
            lines.append("Pickup window times: %s – %s" % (
                shipment["pickup_earliest"] or "?",
                shipment["pickup_latest"] or "?"))
        if shipment["delivery_deadline"]:
            lines.append("Delivery deadline: before %s"
                         % shipment["delivery_deadline"])
        if shipment["service_minutes"]:
            lines.append("Service time at delivery: %s minutes"
                         % shipment["service_minutes"])
        if shipment["stops"]:
            lines.append("Additional stops: %s" % shipment["stops"])
        if shipment["accessorials"]:
            lines.append("Accessorials: %s" % shipment["accessorials"])
        if shipment["contacts"]:
            lines.append("Contacts: %s" % shipment["contacts"])
        if shipment["instructions"]:
            lines.append("Instructions: %s" % shipment["instructions"])
        if shipment["document_number"]:
            lines.append("Document number: %s" % shipment["document_number"])
        if shipment["temperature_mode"] == "reefer" \
                and not shipment["temperature_setpoint_stated"]:
            lines.append(_(
                "Reefer setpoint was not numerically stated — confirm the "
                "setpoint with the customer before pricing."))
        return lines

    def rc_populate_vals(self, stops, shipment):
        """logistics.custom.quote values from resolved stops + shipment.

        customer_po is deliberately NOT here: the CQ lifecycle's only
        sanctioned PO source is the customer-supplied PO on the lead
        (logistics_custom_quote._prepare_from_lead, D-B2 §6) — free-text
        extraction never writes it.
        """
        return {
            "pickup_address": stops["pickup"]["address"],
            "pickup_postal_code": stops["pickup"]["postal_code"],
            "delivery_address": stops["delivery"]["address"],
            "delivery_postal_code": stops["delivery"]["postal_code"],
            "pallets": shipment["pallets"],
            "weight_lbs": shipment["weight_lbs"],
            "commodity": shipment["commodity"] or "",
            "load_type": shipment["load_type"],
            "temperature_mode": shipment["temperature_mode"],
            "required_temperature_c": shipment["required_temperature_c"]
                if shipment["temperature_mode"] == "reefer"
                and shipment["temperature_setpoint_stated"] else False,
            "requested_pickup_date": shipment["requested_pickup_date"],
            "notes": "\n".join(
                ["Prepared from the CRM opportunity by 'Create Draft Rate "
                 "Confirmation' — shipment facts below come from the LATEST "
                 "customer statements (newer emails supersede older ones). "
                 "Draft only: nothing was priced, sent or booked.",
                 *self.rc_notes_lines(shipment)]),
        }

    # ════════════════════════════════════════════════════════════════════
    # Price request (canonical path only — contract §5)
    # ════════════════════════════════════════════════════════════════════

    def estimate_request_values(self, lead, stops, shipment):
        """Raw request values for BookingOrchestrationService — the SAME
        shape the phone wizard sends (pricing_method corridor)."""
        return {
            "partner_id": lead.partner_id.id,
            "source_model": "crm.lead",
            "source_res_id": lead.id,
            "pickup_stops": [self._stop_payload(
                stops["pickup"], shipment, lead.partner_id)],
            "delivery_stops": [self._stop_payload(
                stops["delivery"], shipment, lead.partner_id)],
            "pallets": shipment["pallets"],
            "physical_pallets": shipment["pallets"],
            "weight_lbs": shipment["weight_lbs"],
            "load_type": shipment["load_type"],
            "equipment_type": shipment["temperature_mode"],
            "required_temperature_c": shipment["required_temperature_c"]
                if shipment["temperature_mode"] == "reefer"
                and shipment["temperature_setpoint_stated"] else None,
            "submitted_temperature_unit": "c",
            "requested_pickup_date": shipment["requested_pickup_date"],
            "pricing_method": "corridor",
            "transfer_allowed": shipment["load_type"] != "ftl",
            "liftgate_pickup": False,
            "liftgate_delivery": False,
            "appointment": False,
            "residential": False,
            "same_day_requested": False,
            "idempotency_key": "crm-estimate:%s" % lead.id,
        }

    def canonical_quote(self, lead, request_values):
        """normalize + prepare_quote — the ONLY sanctioned price source.

        Never indexes the result blindly: a missing/non-numeric price raises
        a clear UserError instead of a raw KeyError/TypeError surfacing to
        the caller."""
        from ..services.booking_orchestration_service import (
            BookingOrchestrationService,
        )
        service = BookingOrchestrationService(self.env)
        request = service.normalize_request(
            request_values, source_channel="internal")
        quote = service.prepare_quote(request)
        if not quote or not quote.get("quote_token"):
            raise UserError(_(
                "The dispatch pricing service returned no quote. Nothing "
                "was created — try again or quote manually via 'Calculate "
                "Dispatch Rate'."))
        amount = quote.get("calculated_price")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise UserError(_(
                "The dispatch pricing service returned quote %(token)s "
                "without a price for this shipment. Nothing was created — "
                "try again or quote manually via 'Calculate Dispatch "
                "Rate'.",
                token=quote.get("quote_token")))
        return quote

    def price_reference_from_quote(self, quote):
        """Human provenance string for the amount that was priced."""
        parts = [part for part in (
            "Dispatch quote %s" % quote.get("quote_token"),
            quote.get("service_offering_name") or "",
            quote.get("lane_name") or "",
            "pickup %s" % quote.get("pickup_date")
            if quote.get("pickup_date") else "",
        )]
        return " — ".join(part for part in parts if part)

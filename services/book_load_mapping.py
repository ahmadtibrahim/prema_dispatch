"""ONE shared deterministic Book Load mapping service.

Used by BOTH Book Load wizards — prema.dispatch.so.book.wizard
(sale.order) and prema.dispatch.book.load.wizard (account.move) — so a
load booked from either source document carries the same field mapping.

Mapping ladder (per field, first confident hit wins, never AI merely to
copy data):

  1. source structured columns   (premafirm_po / premafirm_bol / ref /
     pickup_city / delivery_city / dates / partner)
  2. stored AI result            (premafirm.ai.baseline snapshot top-level
     keys, then the service-note text the AI flow wrote on the invoice /
     order line — parsed deterministically, never re-asked from the LLM)
  3. line details                (freight product display names: service
     kind LTL/FTL + equipment Reefer/Dry/Flatbed; non-freight product
     names as commodity)
  4. saved-location resolution   (city/province text -> exactly one active
     saved location of the customer or global; ambiguous -> left blank)

Ladder 2 also carries the shipment figures the AI flow already wrote into
the service-note text as LABELLED FIELDS — ``Load: N pallets / W lb`` and
``Commodity: …``. Those are the stored extraction result, read back
verbatim (comma thousands separators accepted); they are only ever used
when the label is actually present, and a temperature carried from the
same stored text sets ``temperature_confirmed`` because the value came
from the customer's own document rather than a keystroke. Nothing is
inferred from a bare number: a figure with no label is left to the
dispatcher.

Fields with NO deterministic source (requires_liftgate) are deliberately
NEVER guessed: the dispatcher enters them and the wizard validation
confirms them. Attachments/AI remain available as a separate
user-invoked step on the source document (engine invoice/quote AI) whose
stored results this service then maps on the next open — AI is never
invoked to copy data that the cascade already produced.
"""

import base64
import io
import re
from datetime import datetime

import pytz

try:  # optional: the tender reader degrades to the text ladders without it
    from pdfminer.high_level import extract_text
except Exception:  # pragma: no cover
    extract_text = None

_FREIGHT_KIND_RE = re.compile(r"\b(LTL|FTL|Dedicated)\b", re.IGNORECASE)
_EQUIPMENT_RE = re.compile(
    r"\b(Reefer|Dry Van|Dry|Frozen|Flatbed|Ambient)\b", re.IGNORECASE)
_TEMPERATURE_RE = re.compile(
    r"(-?\d{1,3}(?:\.\d+)?)\s*(?:°)?\s*([CF])\b", re.IGNORECASE)
_DATE_RE = re.compile(r"Date:\s*([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})")
# Labelled shipment figures written by the AI flow into the service note
# ("Load: 12 pallets / 12,000 lb", "Commodity: FROZEN BAKERY"). The label
# is what makes the value deterministic — an unlabelled number is ignored.
_LOAD_RE = re.compile(
    r"Load:\s*(\d[\d,]*)\s*(?:pallets?|skids?)\s*/\s*"
    r"(\d[\d,]*(?:\.\d+)?)\s*(?:lb|lbs|pounds)\b", re.IGNORECASE)
_LOAD_PROSE_RE = re.compile(
    r"(\d[\d,]*)\s*(?:pallets?|skids?)\s*\(\s*"
    r"(\d[\d,]*(?:\.\d+)?)\s*(?:lb|lbs|pounds)\s*\)", re.IGNORECASE)
_COMMODITY_RE = re.compile(
    r"Commodity:\s*([^\n\r]{2,80}?)\s*(?=\n|\r|$)", re.IGNORECASE)
_PICKUP_WINDOW_RE = re.compile(
    r"Pickup:\s*(\d{1,2}:\d{2})\s*(AM|PM)", re.IGNORECASE)
# ── TIER 2 timing (§2/§3): appointment windows, facility hours, deadline ──
_TIME = r"(\d{1,2}:\d{2})\s*(AM|PM)"
# "11:00 AM–12:00 PM", "2:30 PM - 3:30 PM", "02:30 PM to 03:30 PM",
# "08:00 to 12:00" (24h) — the separators real tenders use.
_SEP = r"\s*(?:–|—|—|-|to|until|thru|through)\s*"
_PICKUP_APPT_RE = re.compile(
    r"Pickup:\s*(?:Appointment\s+required\s+)?%s%s%s" % (_TIME, _SEP, _TIME),
    re.IGNORECASE)
_DELIVERY_APPT_RE = re.compile(
    r"Delivery:\s*(?:Appointment\s+required\s+)?%s%s%s" % (_TIME, _SEP, _TIME),
    re.IGNORECASE)
# The bare "Appointment required 11:00 AM to 12:00 PM" form used by the
# carrier tender's Accessorials line (side resolved by the section).
_ANY_APPT_RE = re.compile(
    r"Appointment\s+required\s+%s%s%s" % (_TIME, _SEP, _TIME), re.IGNORECASE)
_HOURS_RE = re.compile(
    r"Hours\s+%s%s%s" % (_TIME, _SEP, _TIME), re.IGNORECASE)
_APPT_REQUIRED_RE = re.compile(
    r"appointments?\s+required", re.IGNORECASE)
_PICKUP_APPT_REQ_RE = re.compile(
    r"pickup[^.\n]{0,80}?appointments?\s+required", re.IGNORECASE)
_DELIVERY_APPT_REQ_RE = re.compile(
    r"delivery[^.\n]{0,80}?appointments?\s+required", re.IGNORECASE)
_BOTH_APPT_REQ_RE = re.compile(
    r"pickup\s+and\s+delivery\s+appointments?\s+required", re.IGNORECASE)
# "STOP 1 - PICKUP" / "STOP 2 - DELIVERY" section headers on a tender PDF.
_SECTION_RE = re.compile(
    r"STOP\s+\d+\s*[-–—]\s*(PICKUP|DELIVERY)", re.IGNORECASE)
# A dock whose documented "hours" span less than this is not a facility
# window (it is a slot/appointment artefact) — see _valid_facility_hours.
_MIN_FACILITY_SPAN_HOURS = 2.0
_ROUTE_RE = re.compile(
    r"Route:\s*([^\n→]{2,90}?)\s*→\s*([^\n]{2,90})", re.IGNORECASE)
_FROM_TO_RE = re.compile(
    r"from\s+([^,]{2,60}?)\s*,\s*([A-Za-z]{2})\s+to\s+([^,]{2,60}?)\s*,"
    r"\s*([A-Za-z]{2})", re.IGNORECASE)
_PROVINCE_TAIL_RE = re.compile(r"\s*,\s*[A-Za-z]{2}\s*$")
_STATE_OR_PROVINCE_RE = re.compile(r"^\s*[A-Za-z]{2}\s*$")

_FREIGHT_NOTE_HEAD = "Freight / Delivery Service"

# Model names the service understands (duck-typed column reads otherwise).
SOURCE_MODELS = ("sale.order", "account.move")


class BookLoadMappingService:
    """Deterministic Book Load mapping for sale.order / account.move."""

    def __init__(self, env):
        self.env = env

    # ── public API ────────────────────────────────────────────────────

    def suggest_for(self, source):
        """Return ``{wizard_field: value}`` for every field the cascade can
        derive confidently from ``source`` (sale.order or account.move).
        Absent keys = left to the dispatcher. Pure read — never writes,
        never invokes AI."""
        self._check_source(source)
        out = {}
        self._map_references(source, out)
        self._map_cities(source, out)
        self._map_line_details(source, out)
        self._map_document_figures(source, out)
        self._map_schedule(source, out)
        self._map_timing(source, out)
        self._resolve_locations(source, out)
        return out

    def summary_for(self, source, mapped):
        """Human lines for the wizard: what was auto-filled and from where,
        plus the fields the dispatcher must still enter."""
        lines = []
        labels = {
            "purchase_order": "PO",
            "bol_reference": "BOL",
            "customer_reference": "Customer reference",
            "service_type": "Service",
            "equipment_type": "Equipment",
            "required_temperature_c": "Reefer temperature",
            "commodity": "Commodity",
            "expected_skids": "Pallets",
            "total_weight_lbs": "Weight (lb)",
            "scheduled_pickup": "Pickup date/time",
            "pickup_saved_location_id": "Pickup address",
            "delivery_saved_location_id": "Delivery address",
            "pickup_window_type": "Pickup window type",
            "pickup_window_start": "Pickup window start",
            "pickup_window_end": "Pickup window end",
            "pickup_appointment_required": "Pickup appointment",
            "pickup_facility_open_time": "Pickup facility open",
            "pickup_facility_close_time": "Pickup facility close",
            "delivery_window_type": "Delivery window type",
            "delivery_window_start": "Delivery window start",
            "delivery_window_end": "Delivery window end",
            "delivery_deadline": "Delivery deadline",
            "delivery_appointment_required": "Delivery appointment",
            "delivery_facility_open_time": "Delivery facility open",
            "delivery_facility_close_time": "Delivery facility close",
        }
        window_types = {
            "flexible": "Flexible", "facility_hours": "Facility Hours",
            "earliest_time": "Earliest Time", "time_window": "Time Window",
            "exact_appointment": "Exact Appointment", "deadline": "Deadline",
        }
        def _display(field, value):
            if field.endswith("_saved_location_id"):
                loc = self.env["prema.dispatch.location"].browse(value)
                return "%s (%s)" % (loc.display_name, loc.city) if loc else value
            if field == "scheduled_pickup":
                return str(value)[:16].replace("T", " ")
            if field.endswith("_window_type"):
                return window_types.get(value, value)
            if field == "delivery_deadline":
                return str(value)[:16].replace("T", " ")
            if field == "required_temperature_c":
                suffix = " (confirmed)" if mapped.get(
                    "temperature_confirmed") else ""
                return "%s °C%s" % (value, suffix)
            if field == "total_weight_lbs":
                return "%g lb" % value
            if field != "expected_skids" and isinstance(value, (int, float)) \
                    and not isinstance(value, bool):
                # Every remaining numeric timing field is a 24h float the
                # document quoted — window starts/ends and the facility's
                # own open/close. (Counts, weights and the temperature are
                # consumed above, so this can never eat them.)
                minutes = int(round(float(value) * 60))
                return "%02d:%02d" % ((minutes // 60) % 24, minutes % 60)
            return value
        for field in (
                "purchase_order", "bol_reference", "customer_reference",
                "service_type", "equipment_type", "required_temperature_c",
                "commodity", "expected_skids", "total_weight_lbs",
                "scheduled_pickup", "pickup_saved_location_id",
                "delivery_saved_location_id",
                "pickup_window_type", "pickup_window_start",
                "pickup_window_end", "pickup_appointment_required",
                "pickup_facility_open_time", "pickup_facility_close_time",
                "delivery_window_type", "delivery_window_start",
                "delivery_window_end", "delivery_deadline",
                "delivery_appointment_required",
                "delivery_facility_open_time", "delivery_facility_close_time"):
            if field in mapped:
                lines.append("%s auto-filled: %s" % (
                    labels[field], _display(field, mapped[field])))
        for field in ("expected_skids", "total_weight_lbs"):
            if field not in mapped:
                lines.append(
                    "%s: enter manually (no deterministic source)."
                    % {"expected_skids": "Pallets",
                       "total_weight_lbs": "Weight (lb)"}[field])
        return lines

    # ── ladder 1 + 2: references from columns and the stored AI result ─

    def _map_references(self, source, out):
        """References only ever come from PO/BOL-bearing sources — the
        document's own number is never passed off as a customer PO."""
        so = source._name == "sale.order"
        po = source.premafirm_po or ""
        if not po and so:
            # SO-side convention (pre-existing): the customer's order
            # reference is their PO on the freight quote.
            po = source.client_order_ref or ""
        if po:
            out["purchase_order"] = po
        bol = getattr(source, "premafirm_bol", "") or ""
        if bol:
            out["bol_reference"] = bol
        cust = (source.client_order_ref if so else source.ref) or source.name
        if cust:
            out["customer_reference"] = cust
        # Stored AI result may hold refs the columns do not yet carry
        # (columns are only filled by the engine apply step).
        snap = self._snapshot(source)
        if not out.get("purchase_order") and snap.get("premafirm_po"):
            out["purchase_order"] = snap["premafirm_po"]
        if not out.get("bol_reference") and snap.get("premafirm_bol"):
            out["bol_reference"] = snap["premafirm_bol"]

    def _snapshot(self, source):
        try:
            snap = self.env["premafirm.ai.baseline"].get_dict(
                source._name, source.id)
        except Exception:
            return {}
        return snap or {}

    # ── ladder 1 + 2: pickup/delivery city text ───────────────────────

    def _map_cities(self, source, out):
        so = source._name == "sale.order"
        cities = {}
        if so:
            if source.pickup_city:
                cities["pickup"] = self._city_prov(source.pickup_city)
            if source.delivery_city:
                cities["delivery"] = self._city_prov(source.delivery_city)
        # Fall back to the stored AI note "Route: A → B" (engine wrote this
        # text on the freight line once; parsing it is deterministic).
        note = self._service_note(source)
        if not cities.get("pickup") or not cities.get("delivery"):
            route = self._route_from(note)
            if route:
                cities.setdefault("pickup", route[0])
                cities.setdefault("delivery", route[1])
        if cities:
            out["_cities"] = cities

    # ── ladder 3: freight products + temperature + commodity ──────────

    def _map_line_details(self, source, out):
        freight_kind = equipment = None
        commodity_names = []
        note = self._service_note(source)
        for line in self._product_lines(source):
            display = (line.product_id.display_name or "") + " " + (
                line.name or "")
            kind = _FREIGHT_KIND_RE.search(display)
            equip = _EQUIPMENT_RE.search(display)
            if kind and kind.group(1).lower() in ("ltl", "ftl", "dedicated"):
                freight_kind = kind.group(1).lower()
            if equip:
                equipment = self._canonical_equipment(equip.group(1))
            if not self._is_freight_product(line):
                name = (line.product_id.display_name or line.name or "").strip()
                if name and not name.startswith(_FREIGHT_NOTE_HEAD):
                    commodity_names.append(name)
        # Service type: LTL/FTL token on a freight product is authoritative.
        if freight_kind and not out.get("service_type"):
            out["service_type"] = freight_kind
        elif freight_kind is None:
            kind = _FREIGHT_KIND_RE.search(note or "")
            if kind and kind.group(1).lower() in ("ltl", "ftl", "dedicated"):
                out["service_type"] = kind.group(1).lower()
        # Equipment + temperature from product text first, then note text.
        if equipment is None and note:
            equip = _EQUIPMENT_RE.search(note)
            if equip:
                equipment = self._canonical_equipment(equip.group(1))
        if equipment and equipment != "dry":
            out["equipment_type"] = equipment
        temperature = self._temperature_from(note or "")
        if temperature is not None:
            out["required_temperature_c"] = temperature
            out["submitted_temperature_unit"] = "c"
            # The figure came from the stored document, not a keystroke:
            # it is carried pre-confirmed. Any edit to the value or the
            # unit clears the box again (wizard onchange), so a corrected
            # temperature is re-confirmed by the dispatcher.
            out["temperature_confirmed"] = True
        if commodity_names:
            seen = []
            for name in commodity_names:
                if name not in seen:
                    seen.append(name)
            out["commodity"] = ", ".join(seen)

    # ── ladder 2: labelled shipment figures from the stored AI note ────

    def _map_document_figures(self, source, out):
        """Pallet count, weight and commodity as the stored AI result
        already recorded them — read back from the labelled lines of the
        service note, never inferred from an unlabelled number."""
        note = self._service_note(source)
        if not note:
            return
        load = self._load_figures(note)
        if load:
            pallets, weight = load
            out.setdefault("expected_skids", pallets)
            out.setdefault("total_weight_lbs", weight)
        # An explicit "Commodity: …" label outranks a product display name
        # (ladder 2 above ladder 3).
        commodity = self._label_value(_COMMODITY_RE, note)
        if commodity:
            out["commodity"] = commodity

    @staticmethod
    def _load_figures(text):
        """``(pallets, weight_lbs)`` from the labelled Load line, else from
        the summary prose's ``N pallets (W lbs)`` form — first hit wins."""
        for pattern in (_LOAD_RE, _LOAD_PROSE_RE):
            match = pattern.search(text)
            if match:
                pallets = BookLoadMappingService._number(match.group(1))
                weight = BookLoadMappingService._number(match.group(2))
                if pallets and pallets > 0 and weight is not None:
                    return int(pallets), weight
        return None

    @staticmethod
    def _number(text):
        try:
            return float(str(text).replace(",", "").strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _label_value(pattern, text):
        match = pattern.search(text)
        if not match:
            return None
        value = " ".join(match.group(1).split())
        if not value:
            return None
        # The AI writes ALL CAPS labels; present them the way the source
        # document does ("FROZEN BAKERY" -> "Frozen Bakery"). Mixed-case
        # values are already human-formatted and left untouched.
        if value.isupper():
            value = value.title()
        return value

    def _map_schedule(self, source, out):
        so = source._name == "sale.order"
        pickup = None
        if so:
            day = source.date_order.date() if source.date_order else None
            if day:
                pickup = self._local_8am(day)
        else:
            resolver = getattr(source, "_resolve_scheduled_pickup", None)
            if callable(resolver):
                try:
                    pickup = source._resolve_scheduled_pickup() or None
                except Exception:
                    pickup = None
            if not pickup and source.invoice_date:
                pickup = self._local_8am(source.invoice_date)
        # A stored AI note can carry the exact "Date: … Pickup: HH:MM AM"
        # — the most precise deterministic schedule available.
        note_pickup = self._pickup_from(self._service_note(source))
        if note_pickup:
            pickup = note_pickup
        if pickup:
            out["scheduled_pickup"] = pickup

    # ── TIER 2 §3: deterministic timing ladder ────────────────────────

    def _map_timing(self, source, out):
        """Pickup/delivery window type + facility hours, by the §3 ladder.

        1. explicit structured source columns  — none exist for shipment
           timing on either source model, so the ladder starts at 2 (the
           reader is written defensively so a future column is picked up);
        2. the LATEST AI extraction stored for this record — but only when
           it is tied to the attachments the record carries NOW;
        3. the line details / service note text (the freight line);
        4. the source's own ATTACHMENTS, and only for whatever is still
           missing (the tender's per-stop "Hours" line).

        A specific appointment is NEVER flattened to Flexible: when a
        window is found the window type follows it. Nothing is guessed —
        a side with no evidence keeps the wizard's own default.
        """
        found = {"pickup": None, "delivery": None,
                 "pickup_hours": None, "delivery_hours": None,
                 "appointment_required": {}}
        for text in self._timing_ladder_texts(source):
            partial = self._timing_from_text(text)
            for key, value in partial.items():
                if key == "appointment_required":
                    for side, flag in value.items():
                        found.setdefault("appointment_required", {})
                        if flag and not found["appointment_required"].get(side):
                            found["appointment_required"][side] = True
                elif value and not found.get(key):
                    found[key] = value
            if found["pickup"] and found["delivery"]:
                break

        # Ladder 4 — the tender attachment, only for gaps the text left.
        if not (found["pickup_hours"] and found["delivery_hours"]
                and found["pickup"] and found["delivery"]):
            for key, value in self._timing_from_attachments(source).items():
                if value and not found.get(key):
                    found[key] = value

        self._emit_timing(found, out)

    def _timing_ladder_texts(self, source):
        """Text blobs in §3 priority order: stored AI extraction (tied to
        the CURRENT attachments), then the line/note text."""
        texts = []
        stored = self._stored_extraction_text(source)
        if stored:
            texts.append(stored)
        note = self._service_note(source)
        if note and note not in texts:
            texts.append(note)
        return texts

    def _stored_extraction_text(self, source):
        """Ladder 2 — the stored AI extraction, returned ONLY when it is
        tied to the attachments this record carries right now. A snapshot
        that names attachments the record no longer has is stale and is
        skipped (the line text then answers, ladder 3)."""
        snap = self._snapshot(source)
        if not snap:
            return ""
        declared = snap.get("source_attachment_ids") or []
        if declared:
            current = set(self._source_attachment_ids(source))
            if current and not (set(declared) & current):
                return ""
        chunks = []
        for key, value in sorted(snap.items()):
            if isinstance(value, str) and key.startswith("line:") \
                    and key.endswith(":name"):
                chunks.append(value)
        summary = getattr(source, "x_ai_summary", "") or ""
        if summary:
            chunks.append(summary)
        return "\n".join(chunks)

    def _timing_from_text(self, text):
        """Windows + per-side appointment flags from one text blob.

        An appointment requirement is only ever attributed per SIDE: the
        window itself is proof, and a side-labelled "…appointment
        required…" sentence is the weaker evidence. The plural phrasing
        ("Pickup and delivery appointments required") names both sides
        explicitly and is the only case that sets both at once.
        """
        text = text or ""
        found = {"appointment_required": {}}
        for side, pattern in (("pickup", _PICKUP_APPT_RE),
                              ("delivery", _DELIVERY_APPT_RE)):
            match = pattern.search(text)
            if match:
                window = self._window_floats(match.group(1), match.group(2),
                                             match.group(3), match.group(4))
                if window:
                    found[side] = window
        for side, pattern in (("pickup", _PICKUP_APPT_REQ_RE),
                              ("delivery", _DELIVERY_APPT_REQ_RE)):
            if pattern.search(text):
                found["appointment_required"][side] = True
        if _BOTH_APPT_REQ_RE.search(text):
            found["appointment_required"].update(
                {"pickup": True, "delivery": True})
        return found

    def _timing_from_attachments(self, source):
        """Ladder 4 — parse the source's own PDF attachments for the
        per-stop 'Hours' line and any appointment the text ladder missed.

        Sectioned tenders name each stop ("STOP 1 - PICKUP"), which is what
        makes a per-side facility window attributable at all. A text 'Hours'
        line with no section is deliberately NOT attributed to a side.
        """
        found = {}
        for text in self._attachment_texts(source):
            sections = self._split_sections(text)
            if not sections:
                continue
            for side, body in sections.items():
                window = found.get(side)
                appointment = (self._timing_from_text(body).get(side)
                               if _ANY_APPT_RE.search(body) else None)
                if appointment and not window:
                    found[side] = appointment
                hours = _HOURS_RE.search(body)
                if hours:
                    span = self._window_floats(*hours.groups())
                    if self._valid_facility_hours(span, appointment or window):
                        found["%s_hours" % side] = span
        return found

    @staticmethod
    def _split_sections(text):
        """{'pickup': body, 'delivery': body} from a sectioned tender."""
        if not text:
            return {}
        markers = list(_SECTION_RE.finditer(text))
        if not markers:
            return {}
        sections = {}
        for index, marker in enumerate(markers):
            end = (markers[index + 1].start()
                   if index + 1 < len(markers) else len(text))
            side = marker.group(1).lower()
            sections.setdefault(side, text[marker.end():end])
        return sections

    @staticmethod
    def _valid_facility_hours(span, appointment):
        """A documented 'Hours' span is the DOCK's hours only when it is a
        real facility window: sane order, at least a couple of hours wide,
        and — when the stop also has an appointment — wide enough to
        contain it. A 30-minute 'hours' line is a slot artefact, not a
        facility (worst case it would tell the ETA engine the dock shuts
        before the appointment it just gave)."""
        if not span:
            return False
        opens, closes = span
        if closes <= opens:
            return False
        if closes - opens < _MIN_FACILITY_SPAN_HOURS:
            return False
        if appointment and not (opens <= appointment[0] and closes >= appointment[1]):
            return False
        return True

    def _attachment_texts(self, source):
        """Decoded text of the source's own PDF attachments (capped, and
        never fatal: an unreadable attachment is skipped)."""
        texts = []
        if extract_text is None:
            return texts
        for attachment in self._source_attachments(source)[:3]:
            try:
                raw = attachment.datas
                if not raw:
                    continue
                stream = io.BytesIO(base64.b64decode(raw))
                if (attachment.mimetype or "").endswith("pdf") or \
                        (attachment.name or "").lower().endswith(".pdf"):
                    texts.append(extract_text(stream) or "")
                else:
                    texts.append(stream.read().decode("utf-8", "replace"))
            except Exception:
                continue
        return texts

    def _source_attachments(self, source):
        attachments = self.env["ir.attachment"].sudo().search([
            ("res_model", "=", source._name), ("res_id", "=", source.id),
        ], order="id desc")
        declared = self._snapshot(source).get("source_attachment_ids") or []
        if declared:
            attachments |= self.env["ir.attachment"].sudo().browse(
                [int(a) for a in declared])
        return attachments

    def _source_attachment_ids(self, source):
        return [a.id for a in self._source_attachments(source)]

    def _emit_timing(self, found, out):
        """Ladder result → wizard fields. The window TYPE follows the
        evidence — a specific appointment is never silently replaced by
        'Flexible' (§3): a quoted appointment window becomes Exact
        Appointment, facility hours alone become Facility Hours, and a
        side with no evidence keeps the wizard's own default."""
        required = found.get("appointment_required") or {}
        for side in ("pickup", "delivery"):
            window = found.get(side)
            hours = found.get("%s_hours" % side)
            if hours:
                out["%s_facility_open_time" % side] = hours[0]
                out["%s_facility_close_time" % side] = hours[1]
            if window:
                out["%s_window_type" % side] = "exact_appointment"
                out["%s_window_start" % side] = window[0]
                out["%s_window_end" % side] = window[1]
                out["%s_appointment_required" % side] = True
            elif hours:
                out["%s_window_type" % side] = "facility_hours"
            if required.get(side) and "%s_appointment_required" % side not in out:
                out["%s_appointment_required" % side] = True

    @staticmethod
    def _window_floats(h1, ap1, h2, ap2):
        """('11:00', 'AM', '12:00', 'PM') → (11.0, 12.0); None if unusable
        or inverted (an inverted window is a parse artefact, not a window)."""
        def _one(hhmm, suffix):
            try:
                hour, minute = (int(part) for part in hhmm.split(":"))
            except (TypeError, ValueError):
                return None
            if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                return None
            hour %= 12
            if (suffix or "").upper() == "PM":
                hour += 12
            return hour + minute / 60.0
        start = _one(h1, ap1)
        end = _one(h2, ap2)
        if start is None or end is None or end <= start:
            return None
        return (start, end)

    # ── ladder 4: city text → exactly one saved location ──────────────

    def _resolve_locations(self, source, out):
        cities = out.pop("_cities", {})
        if not cities:
            return
        partner = self._partner(source)
        for side, (city, province) in cities.items():
            location = self._resolve_location(city, province, partner)
            if location:
                out["%s_saved_location_id" % side] = location.id

    # ── helpers ───────────────────────────────────────────────────────

    def _check_source(self, source):
        if not source or source._name not in SOURCE_MODELS:
            raise ValueError(
                "BookLoadMappingService expects a sale.order or account.move"
                " record, got %r" % (source._name if source else None))

    def _partner(self, source):
        if source._name == "sale.order":
            return source.partner_invoice_id or source.partner_id
        return source.partner_id

    def _product_lines(self, source):
        if source._name == "sale.order":
            return source.order_line.filtered("product_id")
        return source.invoice_line_ids.filtered(
            lambda l: l.display_type in (False, "product") and l.product_id)

    def _service_note(self, source):
        """Concatenated service-note text (the freight line description) +
        stored AI snapshot note text + summary prose, newest/longest last —
        deterministic string sources for regex parsing. The note may sit on
        a product-less line (S00102), so every line is scanned for it."""
        chunks = []
        if source._name == "sale.order":
            lines = source.order_line
        else:
            lines = source.invoice_line_ids
        for line in lines:
            name = line.name or ""
            if name.startswith(_FREIGHT_NOTE_HEAD) or "Route:" in name:
                chunks.append(name)
        snap = self._snapshot(source)
        for key, value in sorted(snap.items()):
            if isinstance(value, str) and (
                    key.startswith("line:") and key.endswith(":name")
                    or key in ("ref",)):
                chunks.append(value)
        summary = getattr(source, "x_ai_summary", "") or ""
        if summary:
            chunks.append(summary)
        return "\n".join(chunks)

    @staticmethod
    def _city_prov(text):
        parts = [p.strip() for p in str(text or "").split(",")]
        city = parts[0] if parts else ""
        province = ""
        for part in parts[1:]:
            if _STATE_OR_PROVINCE_RE.match(part):
                province = part.upper()
        return (city, province)

    @staticmethod
    def _route_from(text):
        for match in _ROUTE_RE.finditer(text):
            left, right = match.group(1).strip(), match.group(2).strip()
            return (BookLoadMappingService._city_prov(left),
                    BookLoadMappingService._city_prov(right))
        for match in _FROM_TO_RE.finditer(text):
            return ((match.group(1).strip(), match.group(2).upper()),
                    (match.group(3).strip(), match.group(4).upper()))
        return None

    @staticmethod
    def _canonical_equipment(token):
        token = token.lower()
        if token in ("reefer", "frozen"):
            return "reefer"
        if token in ("dry", "dry van", "ambient"):
            return "dry"
        if token == "flatbed":
            return "flatbed"
        return None

    @staticmethod
    def _is_freight_product(line):
        name = (line.product_id.display_name or "") + " " + (line.name or "")
        return bool(_FREIGHT_KIND_RE.search(name)) or (
            name or "").startswith(_FREIGHT_NOTE_HEAD)

    def _temperature_from(self, text):
        if not text:
            return None
        found = None
        for match in _TEMPERATURE_RE.finditer(text):
            value = float(match.group(1))
            unit = match.group(2).upper()
            # A °F claim next to a °C claim would be ambiguous: take the
            # LAST explicit unit; convert F to canonical C.
            found = (value, unit)
        if not found:
            return None
        value, unit = found
        if unit == "F":
            try:
                from odoo.addons.prema_logistics_booking.services.temperature_service import (  # noqa: E501
                    parse_temperature)
                return parse_temperature(value, unit="f")
            except Exception:
                return round((value - 32) * 5.0 / 9.0, 1)
        return value

    def _pickup_from(self, text):
        """``Date: <month d, yyyy>`` + ``Pickup: HH:MM AM`` -> naive-UTC
        datetime (same convention as the wizard's 8 AM default)."""
        if not text:
            return None
        date_match = _DATE_RE.search(text)
        time_match = _PICKUP_WINDOW_RE.search(text)
        if not date_match or not time_match:
            return None
        try:
            day = datetime.strptime(
                date_match.group(1).replace(",", ""), "%B %d %Y").date()
        except ValueError:
            try:
                day = datetime.strptime(
                    date_match.group(1).replace(",", ""), "%b %d %Y").date()
            except ValueError:
                return None
        hour = int(time_match.group(1).split(":")[0]) % 12
        minute = int(time_match.group(1).split(":")[1])
        if time_match.group(2).upper() == "PM":
            hour += 12
        return self._local_to_utc_naive(day, hour, minute)

    def _local_to_utc_naive(self, day, hour, minute):
        tz_name = self.env.context.get("tz") or self.env.user.tz or "UTC"
        try:
            local_tz = pytz.timezone(tz_name)
        except Exception:
            local_tz = pytz.utc
        local_dt = local_tz.localize(datetime(day.year, day.month, day.day,
                                              hour, minute))
        return local_dt.astimezone(pytz.utc).replace(tzinfo=None)

    def _local_8am(self, day):
        return self._local_to_utc_naive(day, 8, 0)

    def _resolve_location(self, city, province, partner):
        if not city:
            return None
        domain = [
            ("active", "=", True),
            ("city", "=ilike", city.strip()),
            "|", ("partner_id", "=", partner.id if partner else False),
            ("partner_id", "=", False),
        ]
        if province:
            domain.append(("province_code", "=ilike", province))
        candidates = self.env["prema.dispatch.location"].search(domain,
                                                                limit=2)
        if len(candidates) == 1:
            return candidates
        return None

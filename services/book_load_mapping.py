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

Fields with NO deterministic source (expected_skids, total_weight_lbs,
requires_liftgate, temperature_confirmed) are deliberately NEVER guessed:
the dispatcher enters them and the wizard validation confirms them.
Attachments/AI remain available as a separate user-invoked step on the
source document (engine invoice/quote AI) whose stored results this
service then maps on the next open — AI is never invoked to copy data
that the cascade already produced.
"""

import re
from datetime import datetime

import pytz

_FREIGHT_KIND_RE = re.compile(r"\b(LTL|FTL|Dedicated)\b", re.IGNORECASE)
_EQUIPMENT_RE = re.compile(
    r"\b(Reefer|Dry Van|Dry|Frozen|Flatbed|Ambient)\b", re.IGNORECASE)
_TEMPERATURE_RE = re.compile(
    r"(-?\d{1,3}(?:\.\d+)?)\s*(?:°)?\s*([CF])\b", re.IGNORECASE)
_DATE_RE = re.compile(r"Date:\s*([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})")
_PICKUP_WINDOW_RE = re.compile(
    r"Pickup:\s*(\d{1,2}:\d{2})\s*(AM|PM)", re.IGNORECASE)
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
        self._map_schedule(source, out)
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
            "scheduled_pickup": "Pickup window",
            "pickup_saved_location_id": "Pickup address",
            "delivery_saved_location_id": "Delivery address",
        }
        def _display(field, value):
            if field.endswith("_saved_location_id"):
                loc = self.env["prema.dispatch.location"].browse(value)
                return "%s (%s)" % (loc.display_name, loc.city) if loc else value
            if field == "scheduled_pickup":
                return str(value)[:16].replace("T", " ")
            if field == "required_temperature_c":
                return "%s °C" % value
            return value
        for field in (
                "purchase_order", "bol_reference", "customer_reference",
                "service_type", "equipment_type", "required_temperature_c",
                "commodity", "scheduled_pickup", "pickup_saved_location_id",
                "delivery_saved_location_id"):
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
        if commodity_names:
            seen = []
            for name in commodity_names:
                if name not in seen:
                    seen.append(name)
            out["commodity"] = ", ".join(seen)

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

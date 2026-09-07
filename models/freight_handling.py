"""Driver freight-handling journal (TODO 8 — driver loose-freight +
pallet-building actions).

One row per auditable freight-handling action a driver records while
working a stop in the Driver App:

    loaded_loose        — loose/unpacked cargo loaded onto the truck as-is
                          (floor-loaded, hand-bombed at delivery)
    built_pallet        — loose cargo built onto CARRIER pallets on the
                          dock: loose freight converts to palletized
    shipper_palletized  — cargo the SHIPPER palletized before pickup
                          (booked loose/mixed, arrived on shipper pallets)
    exception           — problem noted on the freight (note + photo only;
                          no cargo conversion, no capacity delta)

Rows are immutable by design — the same ACL-only convention as
prema.dispatch.evidence: the driver ACL row is read-only (1,0,0,0), rows
are created under sudo() by callers that already authorized the driver
for the stop, and nothing in the product updates or deletes a row after
creation (no write/unlink override is needed because no ACL grants them).

Every event is the single audit record of its action; the booking-line
and dispatch-item state it produced are written beside it in the same
transaction (so they can never diverge), and its capacity/billing
implications are exposed through _capacity_delta() for the pricing
engine that consumes this journal.
"""
import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

FREIGHT_HANDLING_ACTIONS = [
    ("loaded_loose", "Loaded Loose"),
    ("built_pallet", "Built Pallet"),
    ("shipper_palletized", "Shipper Palletized"),
    ("exception", "Exception"),
]

# Dispatch-item load_unit_type values that represent UNPACKED freight
# (mirrors the loose/mixed vocabulary TODO 7 books and creates items with).
# These are the items the three cargo actions apply to; fully palletized
# cargo never offers them and the server refuses a conversion on them.
LOOSE_UNIT_TYPES = ("loose", "carton", "tote", "other")

# load_unit_type values that occupy a floor position — the predicate
# dispatch_item._compute_consumes_floor_position uses. A converted item
# lands in this set so the existing load-plan predicates
# (plan.add_job's floor filter, job/plan count computes, guide steps 2-3)
# admit it unchanged; NOTHING here edits those predicates.
FLOOR_UNIT_TYPES = ("pallet", "shared_pallet", "container")

# Freight actions that convert the dispatch item loose → pallet.
_CONVERTING_ACTIONS = ("built_pallet", "shipper_palletized")


class PremaDispatchFreightHandling(models.Model):
    _name = "prema.dispatch.freight.handling"
    _description = "Dispatch Freight Handling Event"
    _order = "event_at desc, id desc"

    # ── Shipment context ─────────────────────────────────────────────
    booking_id = fields.Many2one(
        "logistics.booking", string="Booking", ondelete="set null", index=True)
    job_id = fields.Many2one(
        "prema.dispatch.job", string="Job", required=True,
        ondelete="cascade", index=True)
    item_id = fields.Many2one(
        "prema.dispatch.item", string="Freight Item", ondelete="set null",
        index=True,
        help="The dispatch freight item (booking-line 1:1) the action "
             "recorded. Set-null on item deletion keeps the audit row.")
    stop_id = fields.Many2one(
        "prema.dispatch.stop", string="Stop", ondelete="set null", index=True)
    driver_id = fields.Many2one(
        "res.users", string="Driver", ondelete="set null", index=True,
        tracking=True)

    # ── The action ───────────────────────────────────────────────────
    action = fields.Selection(
        FREIGHT_HANDLING_ACTIONS, string="Action", required=True, index=True)
    event_at = fields.Datetime(
        string="Event At", default=fields.Datetime.now, index=True,
        help="When the driver recorded the action (server time).")

    # ── Captured actuals ─────────────────────────────────────────────
    case_count = fields.Integer(
        string="Cases",
        help="Loaded Loose: cases loaded loose. Built Pallet: cases still "
             "on the floor AFTER building (leftover freight hand-bombed). "
             "Shipper Palletized / Exception: not captured.")
    pallets_built = fields.Integer(
        string="Pallets Built",
        help="Built Pallet: how many pallets the driver built from loose "
             "freight. 0 on every other action.")
    pallets_loaded = fields.Integer(
        string="Pallets Loaded",
        help="Shipper Palletized: how many shipper pallets the freight "
             "arrived on. 0 on every other action.")
    carrier_pallet_used = fields.Boolean(
        string="Carrier Pallet Used",
        help="Built Pallet: the build used a carrier pallet (billable).")
    shrink_wrap_used = fields.Boolean(
        string="Shrink Wrap Used",
        help="Built Pallet: the build was shrink-wrapped.")
    labour_minutes = fields.Float(
        string="Labour Minutes",
        help="Handling labour spent (Loaded Loose / Built Pallet).")
    billable_pallets = fields.Boolean(
        string="Billable Pallets", default=True,
        help="False when the pallets built are NOT billable (e.g. free "
             "restacking). Only meaningful for Built Pallet.")
    billable_labour = fields.Boolean(
        string="Billable Labour", default=True,
        help="False when the handling labour is NOT billable.")
    notes = fields.Text(string="Notes")
    photo_attachment_id = fields.Many2one(
        "ir.attachment", string="Photo", ondelete="set null",
        help="Optional photo of the handled freight (also recorded as a "
             "freight_photo evidence row against the stop).")

    # ── Capacity/billing semantics ───────────────────────────────────

    def _capacity_delta(self):
        """What this event adds to the truck's load and to the customer's
        bill (pallet slots, billable pallets, billable labour minutes,
        billable cases). Consumed by the capacity/billing engine — the
        Driver App never reads this directly.

        Rules (TODO 8 spec):
          * loaded_loose       — freight rides as loose cargo: no pallet
                                 slot, billed by the cases loaded.
          * built_pallet       — freight now occupies floor slots (pallet
                                 count) and the labour/carrier pallet that
                                 built it is billed; cases are NEVER billed
                                 again on top of built pallets (the cases
                                 still on the floor are hand-bomb freight —
                                 covered by the labour minutes, not by
                                 case billing).
          * shipper_palletized — freight occupies the shipper pallets'
                                 slots; pallets belong to the shipper (no
                                 pallet charge); the freight's size measure
                                 stays the line's case count when the line
                                 is the source of truth.
          * exception          — nothing: note + photo only.
        """
        self.ensure_one()
        labour = (self.labour_minutes or 0.0) if self.billable_labour else 0.0
        if self.action == "loaded_loose":
            return {
                "pallet_slots": 0.0,
                "billable_pallets": 0,
                "billable_labour_minutes": labour,
                "billable_cases": self.case_count or 0,
            }
        if self.action == "built_pallet":
            built = self.pallets_built or 0
            return {
                "pallet_slots": float(built),
                "billable_pallets": built if self.billable_pallets else 0,
                "billable_labour_minutes": labour,
                "billable_cases": 0,
            }
        if self.action == "shipper_palletized":
            line = self._resolve_booking_line(self.item_id) \
                if self.item_id else self.env["logistics.booking.line"]
            # No cases are captured on this action — when a booking line
            # resolved, its case count is the freight's size measure (the
            # same source loose-cargo consumers use); never fabricate one.
            base_cases = self.case_count or 0
            line_cases = (line.case_count or 0) if line else 0
            return {
                "pallet_slots": float(self.pallets_loaded or 0),
                "billable_pallets": 0,
                "billable_labour_minutes": labour,
                "billable_cases": base_cases or line_cases,
            }
        return {
            "pallet_slots": 0.0,
            "billable_pallets": 0,
            "billable_labour_minutes": 0.0,
            "billable_cases": 0,
        }

    # ── Booking-line resolution (item → logistics.booking.line) ──────

    @api.model
    def _resolve_booking_line(self, item):
        """Best-effort mapping from a dispatch freight item back to the
        logistics.booking.line it was created from.

        The booking→dispatch conversion (_create_dispatch_operation)
        stores NO backlink on the item — one item per line — so the
        mapping is re-derived here. Booking-line writes from a freight
        action only happen when the mapping is unambiguous:

          * no booking on the job (manual planner jobs, cross-module
            module-load windows) → no line;
          * single-line booking → that line;
          * multi-line booking → the unique loose line whose cargo
            signature (commodity + weight — the values the conversion
            copied onto the item) matches. Ambiguous or no match → no
            line: an item-only record is safer than writing the wrong
            booking line's billing state.

        Runs in the caller's env; call .sudo() when the caller may lack
        booking ACL (driver contexts)."""
        if not item or not item.exists():
            return self.env["logistics.booking.line"]
        job = item.job_id
        if "logistics_booking_id" not in job._fields or not job.logistics_booking_id:
            return self.env["logistics.booking.line"]
        if "logistics.booking.line" not in self.env.registry.models:
            return self.env["logistics.booking.line"]
        booking = job.logistics_booking_id
        lines = booking.line_ids
        if len(lines) == 1:
            return lines
        desc = (item.description or "").strip()
        weight = item.weight_lbs or 0.0
        candidates = lines.filtered(
            lambda ln: ln._dispatch_load_unit_type() == "loose"
            and (ln.commodity or "").strip() == desc
            and (ln.weight_lbs or 0.0) == weight)
        return candidates if len(candidates) == 1 \
            else self.env["logistics.booking.line"]

    @api.model
    def _freight_booking_payload(self, item):
        """Serialize the booking line an item was created from (sudo-safe
        for the driver app's stop payload — the resolver's guarantees
        apply; False when no unambiguous line exists)."""
        if not item or not item.exists():
            return False
        line = self.sudo()._resolve_booking_line(item)
        if not line:
            return False
        return {
            "handling_type": line.handling_type,
            "description": line.description or "",
            "commodity": line.commodity or "",
            "package_quantity": line.package_quantity or 0,
            "package_uom": line.package_uom or "",
            "case_count": line.case_count or 0,
            "current_load_form": line.current_load_form,
            "pallets_built": line.pallets_built or 0,
            "hand_bomb_required": bool(line.hand_bomb_required),
            "carrier_pallet_used": bool(line.carrier_pallet_used),
            "actual_pallet_count": line.actual_pallet_count or 0,
            "planned_pallet_equivalent": line.planned_pallet_equivalent or 0.0,
        }

    # ── Serialization for the driver app ─────────────────────────────

    def _event_payload(self):
        self.ensure_one()
        att = self.photo_attachment_id
        return {
            "id": self.id,
            "action": self.action,
            "action_label": dict(FREIGHT_HANDLING_ACTIONS).get(
                self.action, self.action),
            "item_id": self.item_id.id,
            "item_label": self.item_id.name if self.item_id else "",
            "job_id": self.job_id.id,
            "stop_id": self.stop_id.id,
            # Naive Odoo Datetime is UTC; "Z" keeps browser parsing
            # unambiguous (same convention as dispatch_job._dt_iso_utc).
            "event_at": (self.event_at.isoformat() + "Z") if self.event_at else "",
            "case_count": self.case_count or 0,
            "pallets_built": self.pallets_built or 0,
            "pallets_loaded": self.pallets_loaded or 0,
            "carrier_pallet_used": bool(self.carrier_pallet_used),
            "shrink_wrap_used": bool(self.shrink_wrap_used),
            "labour_minutes": self.labour_minutes or 0.0,
            "billable_pallets": bool(self.billable_pallets),
            "billable_labour": bool(self.billable_labour),
            "notes": self.notes or "",
            "photo_url": f"/web/content/{att.id}" if att else "",
        }

    @api.model
    def _stop_freight_events_payload(self, stop, limit=12):
        """Most-recent freight-handling events for a stop (bounded — the
        driver app only renders a short history; the full journal lives in
        the backend views)."""
        if not stop or not stop.exists():
            return []
        events = self.sudo().search(
            [("stop_id", "=", stop.id)], order="event_at desc, id desc",
            limit=limit)
        return [ev._event_payload() for ev in events]

    # ── The single entry point ───────────────────────────────────────

    @api.model
    def record_freight_handling(self, stop, action, values=None):
        """Record ONE auditable freight-handling event for `action` at
        `stop` and apply its cargo conversion.

        Called under sudo() from driver_add_freight_handling (the driver
        has already been authorized for the stop there) — this model's own
        rows are driver-read-only, and booking/plan/attachment writes need
        no per-field ACL games.

        `values` keys: item_id / case_count / pallets_built /
        pallets_loaded / carrier_pallet_used / shrink_wrap_used /
        labour_minutes / billable_pallets / billable_labour / notes /
        photo {data_b64, filename, captured_at, lat, lng, device,
        gps_accuracy_m, captured_tz}.

        Conversion rules (TODO 8 spec):
          * built_pallet — the booking line (when resolved unambiguously)
            flips to carrier_palletized with pallets_built += n and
            hand_bomb_required recomputed from the leftover floor cases;
            its ORIGINAL record (case_count, planned_pallet_equivalent,
            actual_pallet_count, handling_type) is never rewritten. The
            dispatch item converts loose → pallet with
            capacity_equivalent = total pallets built for this freight.
          * shipper_palletized — the line flips to shipper_palletized and
            actual_pallet_count is corrected to what the driver saw when
            he entered pallets (intake reality; never zeroed). The item
            converts loose → pallet with capacity_equivalent =
            pallets_loaded (fallback: the line's actual count).
          * loaded_loose — nothing converts: the item stays loose, its
            capacity_equivalent is left as-is (0 = unset is respected —
            this journal never fabricates a footprint); the event's cases
            are the audit record.
          * exception — nothing converts; note + photo only.

        After a conversion the item is attached to its job's active load
        plan and the plan is marked stale (attaching is what the canonical
        pickup-actuals path does; the plan's stored counts recompute
        themselves from the item's new consumes_floor_position via the
        existing dependency chain — no manual recompute here). A layout
        swap is deliberately NOT proposed mid-dock: the driver's own
        pickup-confirm step remains the capacity authority.

        The event row, the line writes, the item conversion, the plan
        state, the photo evidence, the timeline event, the §9 feed row and
        the job chatter note all commit in ONE transaction.
        """
        if not stop or not stop.exists():
            raise UserError("Stop not found.")
        job = stop.job_id
        values = values or {}
        FH = self  # recordset on prema.dispatch.freight.handling (sudo env)
        Item = self.env["prema.dispatch.item"]

        # ── Item + validation ─────────────────────────────────────────
        item_id = values.get("item_id")
        try:
            item = Item.browse(int(item_id or 0)) if item_id else Item
        except (TypeError, ValueError):
            item = Item
        if not item.exists():
            raise UserError("Freight item not found.")
        if item.job_id.id != job.id:
            raise UserError("This freight item does not belong to the stop's job.")
        if item.pickup_stop_id and item.pickup_stop_id.id != stop.id:
            raise UserError("This freight item is not picked up at this stop.")

        if action not in dict(FREIGHT_HANDLING_ACTIONS):
            raise UserError("Unknown freight-handling action.")

        if action in _CONVERTING_ACTIONS:
            if item.load_unit_type not in LOOSE_UNIT_TYPES:
                raise UserError(
                    "This freight is not loose — nothing to convert. Record "
                    "an Exception instead if something is wrong.")
        elif action == "loaded_loose":
            if item.load_unit_type not in LOOSE_UNIT_TYPES:
                raise UserError(
                    "Loaded Loose can only be recorded on loose freight.")

        def _int(key):
            try:
                return int(values.get(key) or 0)
            except (TypeError, ValueError):
                return 0

        def _float(key):
            try:
                return float(values.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        def _bool(key, default=True):
            v = values.get(key)
            if v is None:
                return default
            return bool(v) if not isinstance(v, str) \
                else v.strip().lower() in ("1", "true", "yes", "on")

        case_count = max(0, _int("case_count"))
        pallets_built = max(0, _int("pallets_built"))
        pallets_loaded = max(0, _int("pallets_loaded"))
        if action == "built_pallet" and pallets_built < 1:
            raise UserError("Enter how many pallets you built (at least 1).")
        if action == "shipper_palletized":
            line0 = self._resolve_booking_line(item)
            if pallets_loaded < 1 and not (line0 and line0.actual_pallet_count):
                raise UserError(
                    "Enter how many shipper pallets the freight arrived on.")

        # Resolve the booking line under sudo (caller may be a driver).
        line = self.sudo()._resolve_booking_line(item)
        booking = job.logistics_booking_id if (
            "logistics_booking_id" in job._fields and job.logistics_booking_id
        ) else self.env["logistics.booking"]

        # ── Photo → evidence-style attachment + freight_photo row ─────
        # Reuses the canonical evidence pipeline: decode/validate the
        # upload, dedupe against the stop's photo bucket, create ONE
        # ir.attachment on the stop and ONE evidence row (type
        # freight_photo, item-linked) — exactly like a POPP photo except
        # that it never satisfies pop/pod proof and is never copied to an
        # invoice. The event row then points at the attachment.
        photo_att = False
        photo_evidence = False
        photo = values.get("photo") or {}
        if photo and photo.get("data_b64"):
            from odoo.addons.prema_dispatch.services.dispatch_upload import (
                decode_and_validate, find_duplicate)
            import base64 as b64mod
            try:
                validated = decode_and_validate(
                    photo.get("data_b64"), photo.get("filename") or "freight.jpg",
                    category="freight")
            except Exception as e:
                msg = getattr(e, "message", None) or str(e)
                raise UserError(f"Photo upload failed: {msg}")
            dup = find_duplicate(
                self.env, stop.photo_attachment_ids,
                validated["checksum_sha256"])
            if not dup:
                photo_att = self.env["ir.attachment"].create({
                    "name": validated["filename"],
                    "type": "binary",
                    "datas": b64mod.b64encode(validated["data"]),
                    "res_model": "prema.dispatch.stop",
                    "res_id": stop.id,
                    "mimetype": validated["mimetype"],
                })
                stop.write({"photo_attachment_ids": [(4, photo_att.id)]})
                if item:
                    item.write(
                        {"evidence_attachment_ids": [(4, photo_att.id)]})
                photo_evidence = self.env["prema.dispatch.evidence"].\
                    _create_evidence(
                        photo_att, stop, "freight_photo", {
                            "pallet_id": item.id,
                            "captured_at": photo.get("captured_at"),
                            "lat": photo.get("lat"),
                            "lng": photo.get("lng"),
                            "device": photo.get("device"),
                            "gps_accuracy_m": photo.get("gps_accuracy_m"),
                            "captured_tz": photo.get("captured_tz"),
                            "checksum_sha256": validated["checksum_sha256"],
                        })
            else:
                photo_att = dup
            # dedupe'd retry: point the event at the existing attachment;
            # the evidence row already exists — do not duplicate it.

        # ── Booking-line conversion writes (built_pallet /
        # shipper_palletized; resolved lines only; originals untouched) ─
        line_vals = {}
        if line:
            if action == "built_pallet":
                leftover_cases = case_count
                line_vals = {
                    "current_load_form": "carrier_palletized",
                    "carrier_pallet_used": _bool("carrier_pallet_used")
                    or bool(line.carrier_pallet_used),
                    "pallets_built": (line.pallets_built or 0) + pallets_built,
                    "hand_bomb_required": leftover_cases > 0,
                }
            elif action == "shipper_palletized":
                line_vals = {"current_load_form": "shipper_palletized"}
                if pallets_loaded > 0:
                    # Intake correction: the shipper's pallets are what the
                    # driver physically saw — never zeroed, only raised to
                    # a real count.
                    line_vals["actual_pallet_count"] = pallets_loaded
            if line_vals:
                line.sudo().write(line_vals)

        # ── Dispatch-item conversion (built_pallet / shipper_palletized) ─
        item_converted = False
        if action in _CONVERTING_ACTIONS:
            # Total palletized count for this freight: the line's running
            # total when resolved; otherwise the running total of this
            # item's own built_pallet events (audit-consistent). For
            # shipper pallets the driver's count wins; a resolved line's
            # intake actual is the fallback.
            if action == "built_pallet":
                if line:
                    capacity = float(line.pallets_built or 0)
                else:
                    capacity = float(
                        pallets_built + sum(
                            FH.search([
                                ("item_id", "=", item.id),
                                ("action", "=", "built_pallet"),
                            ]).mapped("pallets_built")))
            else:
                capacity = float(
                    pallets_loaded
                    or (line.actual_pallet_count if line else 0)
                    or 0)
            item.write({
                "load_unit_type": "pallet",
                "capacity_equivalent": capacity,
            })
            item_converted = True
            # The freight is physically at THIS stop — it is no longer a
            # future-pickup reservation (plan payloads, job/plan counts
            # and guide steps exclude pending_future_pickup items; without
            # this the fresh pallets would stay invisible).
            if item.available_after_stop_id \
                    and item.available_after_stop_id.id == stop.id:
                item.write({"available_after_stop_id": False})

        # ── Load-plan sync (conversions only) ─────────────────────────
        # Mirrors the canonical pickup-actuals tail: attach the converted
        # item to the job's active plan and mark the plan stale so the
        # dispatcher re-validates; stored computes (plan counts, job
        # operational counts) refresh themselves through their dependency
        # on the item's consumes_floor_position. No layout is proposed
        # here — evaluate_layout_for_capacity stays with the pickup-confirm
        # step, the capacity authority.
        if item_converted:
            plan = item.load_plan_id
            if not plan and job.vehicle_id and job.scheduled_pickup:
                plan = self.env["prema.dispatch.load.plan"].sudo().search([
                    ("vehicle_id", "=", job.vehicle_id.id),
                    ("operating_date", "=", fields.Date.to_date(
                        job.scheduled_pickup)),
                    ("active", "=", True),
                ], limit=1)
            if plan:
                if not item.load_plan_id:
                    item.write({"load_plan_id": plan.id})
                plan.sudo()._mark_stale(
                    f"{dict(FREIGHT_HANDLING_ACTIONS).get(action, action)} "
                    f"recorded by {self.env.user.name} at "
                    f"{stop.address or stop.stop_type} — freight item "
                    f"{item.name} converted to pallet "
                    f"(capacity {capacity:g}).")
                plan.invalidate_recordset()

        # ── The audit row (single source of truth for the action) ─────
        event = self.create({
            "booking_id": booking.id if booking else False,
            "job_id": job.id,
            "item_id": item.id,
            "stop_id": stop.id,
            "driver_id": self.env.user.id,
            "action": action,
            "case_count": case_count,
            "pallets_built": pallets_built,
            "pallets_loaded": pallets_loaded,
            "carrier_pallet_used": _bool("carrier_pallet_used")
            if action == "built_pallet" else False,
            "shrink_wrap_used": _bool("shrink_wrap_used")
            if action == "built_pallet" else False,
            "labour_minutes": _float("labour_minutes")
            if action in ("loaded_loose", "built_pallet") else 0.0,
            "billable_pallets": _bool("billable_pallets")
            if action == "built_pallet" else True,
            "billable_labour": _bool("billable_labour")
            if action in ("loaded_loose", "built_pallet") else True,
            "notes": (values.get("notes") or "").strip() or False,
            "photo_attachment_id": photo_att.id if photo_att else False,
        })

        # ── Observability: timeline + §9 feed + job chatter ───────────
        label = dict(FREIGHT_HANDLING_ACTIONS).get(action, action)
        detail_bits = []
        if action == "loaded_loose" and case_count:
            detail_bits.append(f"{case_count} cases")
        if action == "built_pallet":
            detail_bits.append(f"{pallets_built} pallet(s) built")
            if case_count:
                detail_bits.append(f"{case_count} case(s) remain on floor")
        if action == "shipper_palletized" and pallets_loaded:
            detail_bits.append(f"{pallets_loaded} shipper pallet(s)")
        if event.labour_minutes:
            detail_bits.append(f"{event.labour_minutes:g} min labour")
        if event.carrier_pallet_used:
            detail_bits.append("carrier pallet")
        if event.shrink_wrap_used:
            detail_bits.append("shrink-wrapped")
        detail = f" — {', '.join(detail_bits)}" if detail_bits else ""
        where = stop.address or stop.stop_type
        notes_txt = f" — {event.notes}" if event.notes else ""
        summary = f"{label} at {where}: {item.name}{detail}{notes_txt}"

        try:
            job.sudo()._post_timeline(
                job, "freight_handled", notes=summary, stop=stop,
                pallet=item, evidence=photo_evidence or False)
        except Exception:
            _logger.exception(
                "Freight-handling timeline failed for job %s", job.id)
        try:
            job._emit_feed(
                "freight_handled", stop=stop, item=item,
                evidence=photo_evidence or False,
                message=summary)
        except Exception:
            _logger.exception(
                "Freight-handling feed failed for job %s", job.id)
        try:
            job.sudo().message_post(
                body=f"📦 {summary}",
                subtype_xmlid="mail.mt_note")
        except Exception:
            _logger.exception(
                "Freight-handling chatter failed for job %s", job.id)
        return event

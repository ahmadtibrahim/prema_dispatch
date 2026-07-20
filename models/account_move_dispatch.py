import re

from odoo import api, fields, models


class AccountMove(models.Model):
    _inherit = "account.move"

    dispatch_job_ids = fields.One2many(
        "prema.dispatch.job", "invoice_id",
        string="Prema Dispatch Jobs", copy=False,
    )
    dispatch_job_count = fields.Integer(
        compute="_compute_dispatch_job_count", store=True
    )
    dispatch_status = fields.Selection([
        ("none",        "No Dispatch"),
        ("draft",       "Draft"),
        ("in_progress", "In Progress"),
        ("completed",   "Completed"),
        ("pod_ready",   "POD Ready"),
        ("posted",      "Posted"),
        ("error",       "Error"),
    ], compute="_compute_dispatch_status", store=True, string="Dispatch Status")
    dispatch_auto_posted = fields.Boolean(
        string="Auto-Posted via Dispatch", readonly=True, copy=False
    )

    # ── Computed ──────────────────────────────────────────────────

    @api.depends("dispatch_job_ids")
    def _compute_dispatch_job_count(self):
        for move in self:
            move.dispatch_job_count = len(move.dispatch_job_ids)

    @api.depends(
        "dispatch_job_ids.stage_id.is_completed",
        "dispatch_job_ids.stage_id.is_cancelled",
        "dispatch_job_ids.pod_complete",
        "dispatch_job_ids.auto_posted_invoice",
        "state",
    )
    def _compute_dispatch_status(self):
        for move in self:
            jobs = move.dispatch_job_ids
            if not jobs:
                move.dispatch_status = "none"
                continue
            if move.state == "posted" and any(j.auto_posted_invoice for j in jobs):
                move.dispatch_status = "posted"
            elif all(j.stage_id and j.stage_id.is_completed for j in jobs):
                move.dispatch_status = (
                    "pod_ready" if all(j.pod_complete for j in jobs)
                    else "completed"
                )
            elif any(j.auto_post_error for j in jobs):
                move.dispatch_status = "error"
            elif all(
                not j.stage_id or j.stage_id.stage_type in ("draft", False)
                for j in jobs
            ):
                move.dispatch_status = "draft"
            else:
                move.dispatch_status = "in_progress"

    # ── Actions ───────────────────────────────────────────────────

    def action_open_dispatch_jobs_prema(self):
        """Smart button — open Prema Dispatch jobs for this invoice."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Dispatch Jobs",
            "res_model": "prema.dispatch.job",
            "view_mode": "list,form",
            "domain": [("invoice_id", "=", self.id)],
            "context": {"default_invoice_id": self.id},
        }

    def action_book_load(self):
        """Book Load button — create Prema Dispatch booking(s) from this invoice.

        If a dispatch job already exists for this invoice, ask the dispatcher
        whether to reopen it or create a separate job, instead of silently
        picking one — this is the one place duplicates were sneaking in when
        someone clicked the button twice or after a job was cancelled/re-run.
        """
        self.ensure_one()

        if self.dispatch_job_ids:
            wizard = self.env["prema.dispatch.duplicate.job.wizard"].create({
                "move_id": self.id,
                "existing_job_ids": [(6, 0, self.dispatch_job_ids.ids)],
                "message": (
                    f"A dispatch job already exists for this invoice "
                    f"({', '.join(self.dispatch_job_ids.mapped('name'))}). "
                    f"Open the existing job or create a separate one?"
                ),
            })
            return {
                "type": "ir.actions.act_window",
                "name": "Dispatch Job Already Exists",
                "res_model": "prema.dispatch.duplicate.job.wizard",
                "res_id": wizard.id,
                "view_mode": "form",
                "target": "new",
            }

        return {
            "type": "ir.actions.act_window",
            "name": "Book Load",
            "res_model": "prema.dispatch.book.load.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"active_id": self.id, "default_move_id": self.id},
        }

    @staticmethod
    def _estimator_core_address_set(estimator):
        """Normalized set of an estimator's stop addresses, with any leading
        business-name segment stripped (e.g. "Carolyn's Liquidations #2, 501
        Ritson Road S, Oshawa, ON" -> "501 ritson road s, oshawa, on") so two
        extractions of the same physical route are recognized as the same
        load even when one includes business names and the other doesn't.
        """
        addresses = set()
        for stop in estimator.stop_ids.filtered(lambda s: not s.is_system):
            addr = (stop.address or "").strip()
            if not addr:
                continue
            parts = [p.strip() for p in addr.split(",")]
            for i, part in enumerate(parts):
                if re.match(r"^\d", part):
                    addr = ", ".join(parts[i:])
                    break
            addresses.add(addr.lower())
        return frozenset(addresses)

    def _do_action_book_load(self):
        self.ensure_one()

        draft_stage = self.env["prema.dispatch.stage"].search(
            [("stage_type", "=", "draft")], limit=1
        )
        estimator_jobs = self.dispatch_estimator_ids

        if estimator_jobs:
            # Book Load must create ONE dispatch job per real physical load,
            # not one per estimator. Two estimators with the same set of stop
            # addresses (once business-name prefixes are stripped) represent
            # the same load re-planned twice — e.g. one AI pass flattened a
            # repeated warehouse pickup into a single stop, a second pass
            # correctly split it into two pickup legs at the same address.
            # Promoting both produced two dispatch jobs for one invoice
            # (the exact bug seen on D-AJX-OSH-WHI-NCL-PTB-CAM-FOX-070624).
            # Only the richest estimator per address-set (most stops, so the
            # repeat-pickup detail is preserved) is promoted; the rest are
            # left untouched (not deleted) and noted in the chatter.
            to_promote = []
            skipped = []
            best_by_addresses = {}
            for est in estimator_jobs.sorted("job_sequence"):
                key = self._estimator_core_address_set(est)
                if not key or key not in best_by_addresses:
                    best_by_addresses[key] = est
                    to_promote.append(est)
                    continue
                kept = best_by_addresses[key]
                if len(est.stop_ids) > len(kept.stop_ids):
                    to_promote[to_promote.index(kept)] = est
                    best_by_addresses[key] = est
                    skipped.append(kept)
                else:
                    skipped.append(est)

            # Second pass: the exact-match check above only catches two
            # estimators covering the SAME address set. It misses the case
            # that actually produced a pickup-only job alongside the real
            # one (DISP/2026/00123 vs 00124) — one estimator's stops were a
            # SUBSET of another's (e.g. just the pickup leg, re-planned
            # separately from the full pickup+deliveries route). Collapse
            # any to-promote estimator whose address set is a proper subset
            # of another to-promote estimator's set, keeping only the
            # fuller (superset) one.
            final = []
            for est in sorted(
                to_promote,
                key=lambda e: -len(self._estimator_core_address_set(e)),
            ):
                est_key = self._estimator_core_address_set(est)
                if est_key and any(
                    est_key < self._estimator_core_address_set(kept)
                    for kept in final
                ):
                    skipped.append(est)
                    continue
                final.append(est)
            to_promote = final

            created = self.env["prema.dispatch.job"]
            for est in to_promote:
                job = self._create_dispatch_job_from_estimator(est, draft_stage)
                created |= job

            if skipped:
                self.message_post(body=(
                    "<b>Book Load</b> — %d job plan(s) were not promoted to a "
                    "dispatch job because they cover the exact same stops as "
                    "another job plan on this invoice (repeated pickup from "
                    "the same warehouse belongs in one job as multiple stops, "
                    "not a separate job): %s"
                ) % (
                    len(skipped),
                    ", ".join(e.job_day_ref or e.name or f"Job {e.job_sequence}" for e in skipped),
                ))
        else:
            # No estimator jobs — create a single blank draft booking
            job = self.env["prema.dispatch.job"].create({
                "invoice_id": self.id,
                "partner_id": self.partner_id.id,
                "ref": self.ref or self.name,
                "stage_id": draft_stage.id if draft_stage else False,
                "company_id": self.company_id.id,
                "dispatcher_id": self.env.uid,
                "source_model": "account.move",
                "source_res_id": self.id,
                "bol_number": self.premafirm_bol or "",
                "po_number": self.premafirm_po or "",
                "scheduled_pickup": self._resolve_scheduled_pickup(),
            })
            created = job

        if len(created) == 1:
            return {
                "type": "ir.actions.act_window",
                "name": "Dispatch Job",
                "res_model": "prema.dispatch.job",
                "res_id": created.id,
                "view_mode": "form",
            }
        return {
            "type": "ir.actions.act_window",
            "name": "Dispatch Jobs",
            "res_model": "prema.dispatch.job",
            "view_mode": "list,form",
            "domain": [("invoice_id", "=", self.id)],
            "context": {"default_invoice_id": self.id},
        }

    def _resolve_scheduled_pickup(self, estimator=None):
        """Pickup Date default for Book Load.

        `estimator.scheduled_at` comes from AI text-extraction, which has
        guessed the wrong YEAR before (e.g. "2024" instead of "2026") since
        nothing told it what today's actual date is at extraction time. A
        job's pickup can't sanely predate the invoice it's booked from by
        more than a few days, so treat an estimator date older than that as
        garbage and fall back to the invoice's own date instead — which is
        what a dispatcher actually expects to see by default.
        """
        import pytz
        from datetime import time as dtime, timedelta

        job_model = self.env["prema.dispatch.job"]
        user_tz = pytz.timezone(self.env.user.tz or "America/Toronto")
        invoice_date = self.invoice_date or self.date or job_model._user_today(user_tz)

        if estimator and estimator.scheduled_at:
            est_local_date = job_model._local_date_of(estimator.scheduled_at, user_tz)
            if est_local_date and est_local_date >= invoice_date - timedelta(days=3):
                return estimator.scheduled_at

        return job_model._local_date_time_to_utc(invoice_date, dtime(8, 0), user_tz)

    @staticmethod
    def _resolve_stop_scheduled_time(resolved_pickup, raw_time):
        """Same stale-year guard as _resolve_scheduled_pickup, applied to
        each individual stop's own scheduled_time.

        A bad AI-extracted year here doesn't corrupt the job's own Pickup
        Date (that's resolved separately), but it silently vanishes the
        stop from the Dispatch Planner — which buckets a stop onto the
        calendar day ITS OWN scheduled_time falls on, in preference to the
        job's date, so a stop dated two years off never appears on any day
        actually being viewed (the exact bug seen on DISP/2026/00125's
        pickup stop). Dropping it to False is the correct fallback, not a
        loss of information: stops with no scheduled_time already default
        to the job's own scheduled_pickup date elsewhere in this module.
        """
        from datetime import timedelta

        if not raw_time:
            return False
        if not resolved_pickup:
            return raw_time
        if abs((raw_time - resolved_pickup).days) > 30:
            return False
        return raw_time

    def _create_dispatch_job_from_estimator(self, estimator, draft_stage):
        """Create a prema.dispatch.job by copying data from a rate.estimator record."""
        stop_type_map = {
            "pickup":   "pickup",
            "delivery": "dropoff",
            "return":   "return",
            "origin":   "pickup",
            "other":    "other",
        }

        vehicle = estimator.vehicle_id
        driver = (
            vehicle.driver_id
            or vehicle.x_current_driver_contact_id
        ) if vehicle else False

        resolved_pickup = self._resolve_scheduled_pickup(estimator)
        job = self.env["prema.dispatch.job"].create({
            "invoice_id": self.id,
            "partner_id": self.partner_id.id,
            "ref": self.ref or self.name,
            "stage_id": draft_stage.id if draft_stage else False,
            "company_id": self.company_id.id,
            "vehicle_id": vehicle.id if vehicle else False,
            "driver_id": driver.id if driver else False,
            "dispatcher_id": self.env.uid,
            "scheduled_pickup": resolved_pickup,
            "internal_notes": estimator.notes or "",
            "service_type": "ltl",
            "source_model": "account.move",
            "source_res_id": self.id,
            "bol_number": self.premafirm_bol or "",
            "po_number": self.premafirm_po or "",
        })

        # Copy non-system stops from estimator. Each stop is created on its
        # own so one bad stop can't silently abort the rest of the loop and
        # leave the job with fewer stops than the estimator actually had.
        failed_stops = []
        for est_stop in estimator.stop_ids.filtered(
            lambda s: not s.is_system
        ).sorted("sequence"):
            mapped_type = stop_type_map.get(est_stop.stop_type, "other")
            try:
                self.env["prema.dispatch.stop"].create({
                    "job_id": job.id,
                    "sequence": est_stop.sequence,
                    "stop_type": mapped_type,
                    "address": est_stop.address or "",
                    "latitude": est_stop.lat,
                    "longitude": est_stop.lng,
                    "scheduled_time": self._resolve_stop_scheduled_time(
                        resolved_pickup, est_stop.scheduled_time
                    ),
                    "pallets_in": est_stop.pallets if mapped_type == "pickup" else 0,
                    "pallets_out": est_stop.pallets if mapped_type == "dropoff" else 0,
                    "pod_required": mapped_type == "dropoff",
                    "driver_notes": est_stop.notes or "",
                })
            except Exception:
                failed_stops.append(est_stop.address or f"stop #{est_stop.sequence}")

        if failed_stops:
            job.message_post(body=(
                "<b>Book Load</b> — %d stop(s) from the source job plan could "
                "not be imported, add them manually: %s"
            ) % (len(failed_stops), ", ".join(failed_stops)))

        return job

    # Backward-compatibility alias — existing Odoo action IDs in the DB still work
    action_create_dispatch_job = action_book_load


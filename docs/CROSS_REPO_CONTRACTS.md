# CROSS-REPO CONTRACTS (dispatch repo)

**Work package:** MP1 D-C1 (master §1 cross-module discipline).
**Status:** consolidated reference — one place for the contracts the
dispatch repo (`prema_dispatch` + `prema_logistics_booking` +
`prema_dispatch_inbox`) must honor toward the engine repo
(`premafirm_ai_engine`).

## 1. Repositories and the dependency rule (non-negotiable)

Two repositories share one Odoo 18 database:

* **engine** — `premafirm_ai_engine` (CRM/sales/invoice heavy; ALL AI runs
  here through `services/deepseek_utils.py`).
* **prema_dispatch repo** — `prema_dispatch`, `prema_logistics_booking`,
  `prema_dispatch_inbox` (canonical pricing, saved locations, bookings,
  planner, driver app).

Dependency direction is strict:
`prema_dispatch` → `prema_logistics_booking` → `premafirm_ai_engine`.
**Engine code never imports, calls, or depends on any dispatch/logistics
model** (that would be a module cycle). Consequences that every package
must respect:

* Cross-repo record types (e.g. `crm.lead`, `crm.recurring.opportunity`,
  `premafirm.lead.estimate.reply`) are extended from the dispatch side —
  precedent: `prema_logistics_booking/models/crm_lead_rate_confirmation.py`.
* The dispatch side may inherit engine views/records by XMLID, never the
  reverse.
* Dispatch-side modules do NOT declare logistics as a hard `depends` when
  the feature is additive (D-C1 SO flow): logistics models are reached by
  runtime lazy import inside methods
  (`from odoo.addons.prema_logistics_booking.services.booking_orchestration_service import BookingOrchestrationService`)
  or by `"logistics.booking" in env.registry` guards.

## 2. Where each contract lives (authoritative documents)

| Contract | Location | Covers |
|---|---|---|
| A2 — estimate-draft + draft RC bridge | engine repo `docs/A2_CROSS_MODULE_CONTRACT.md` | `crm.lead` two deliberate staff actions; `premafirm.lead.estimate.reply.prepare_from_lead(...)`; `logistics.custom.quote.find_or_create_draft_for_lead(lead_id, idempotency_key=...)` — exactly ONE discoverable draft RC per lead; `LeadFactService` supersession |
| A3 — recurring-opportunity bridge | engine repo `docs/A3_RECURRING_BRIDGE_CONTRACT.md` | `crm.recurring.opportunity` (one open record per lead/frequency/partner; kind potential/contracted gated by `customer_confirmed`); dispatch-side `action_activate/pause/expire/cancel` overrides keep engine state in sync; no engine record deletion |
| D-C1 — Sale Order / phone-deprecation contracts | this repo: `docs/CROSS_REPO_CONTRACTS.md`, `prema_logistics_booking/docs/PHONE_BOOKING_PARITY.md` | dependency rule above; SO entry via canonical booking channel `sale_order`; phone wizard kept as legacy with a parity guide |

## 3. Canonical services — single authority (all in prema_logistics_booking)

Every channel MUST enter through `BookingOrchestrationService`
(`services/booking_orchestration_service.py`): direct creation of
`logistics.booking` / booking stops / legs / `account.move` /
`prema.dispatch.job` from controllers/wizards is forbidden (module
docstring doctrine).

* `BookingOrchestrationService.normalize_request(values, source_channel)`
  → `NormalizedBookingRequest`; `confirm_from_internal(...)` → confirmed
  booking (idempotent, atomic, capacity-reserving); `prepare_quote(...)`;
  `cancel_booking(...)`.
* `PricingService.calculate(...)` — corridor/rate-plan/contract pricing.
* `DepartureResolver` (+ `_available_pickup_dates`) — departure/date
  availability used by portal calendar, phone wizard, recurring generator.
* `ShipmentRoutingService`, `RouteFinalizationService`, `CapacityEngine`,
  `services/direct_delivery_service.py` — exact-route resolution,
  finalization and per-departure capacity validation.
* The booking's own `_create_dispatch_job()` / `_create_dispatch_operation()`
  bridge is the ONLY creator of Planner cards; it back-links
  `logistics_booking_id`, `ltl_operation_key` (`booking:{id}:leg:{leg.id}:{role}`
  or `booking:{id}:custom`) and — for SO-originated bookings —
  `sale_order_id` (D-C1).

## 4. Idempotency-key conventions (master log §11, extended D-C1)

Channel-key is enforced by the unique constraint
(`source_channel`, `idempotency_key`) on `logistics.booking`.

| Channel | Key |
|---|---|
| phone | `phone:{wizard_id}` |
| invoice | `invoice:{move_id}:{booking_mode}` |
| custom quote | `custom_quote:{quote_id}` |
| recurring | `recurring-job:{recurring_job_id}:{due.isoformat()}` |
| sale order — Book Load | `sale.order:{so_id}:{booking_mode}` (D-C1) |
| sale order — Generate from Text | `sale.order:{so_id}:text:{fingerprint}` (D-C1; per-text, so a legitimately different shipment on the same SO gets its own booking) |

Channels must be added to both `SOURCE_CHANNELS`
(`booking_orchestration_service.py`) and `BOOKING_CHANNEL_SELECTION`
(`logistics_booking.py`) when new entry points ship.

## 5. Cross-repo duties when touching AI

Engine owns every LLM call (`deepseek_utils.py`); dispatch-side AI features
call engine services (e.g. `InvoiceAIService.analyze_from_text` for the SO
Generate-from-Text flow) and never open their own model connection.
Engine services never receive dispatch model objects — they work on the
dispatch side's data as plain records passed in by the dispatch caller.

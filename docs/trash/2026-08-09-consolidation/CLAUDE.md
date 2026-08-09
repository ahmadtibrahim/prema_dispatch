# Prema Dispatch — File Index

> **NOTE:** This is a file-level index for code navigation. For architecture,
> business rules, pricing, capacity, deployment procedures, and decision history,
> see the authoritative master document:
> **`PREMA_DISPATCH_MASTER.md`** (same directory).

Purpose: let a future session find the right file in one lookup instead of
grepping/exploring. Keep this updated when files are added/removed/renamed.
Module: `prema_dispatch` · Path: `/opt/odoo/custom-addons/prema_dispatch` ·
DB: `Prod-db` · Config: `/etc/odoo18.conf` · Version: `18.0.2.2.0`

**Upgrade command:** `cd /opt/odoo/odoo18 && python3 odoo-bin -c /etc/odoo18.conf -d Prod-db --stop-after-init -u prema_dispatch --no-http`
**Always follow `-u` with `systemctl restart odoo18`** — stale workers is a recurring gotcha in this project.
**Isolated test DB pattern:** `pg_dump 'Prod-db' | psql 'Prod-db-test1a'`, upgrade there first, never `-u` production untested.

## Models (`models/`)

| File | Model(s) (`_name`) | Purpose |
|---|---|---|
| `dispatch_job.py` | `prema.dispatch.job` | Core dispatch job (booking → delivery lifecycle). Also hosts every `driver_*`/`get_driver_*` RPC method the Driver App calls, and the assignment-log write() hook. |
| `dispatch_stop.py` | `prema.dispatch.stop` | Per-stop record: pickup/dropoff/return/transfer/cross_dock_drop/cross_dock_pickup. Geocoding, address validation, POD/POP attachment fields, GPS stamps. |
| `dispatch_item.py` | `prema.dispatch.item` | Physical freight item/skid. Extended in Phase 2 with Load Plan fields (`load_plan_id`, `position_id`, `load_unit_type`, `qr_token`, damage/exception fields). Custody tracking, evidence attachments. |
| `dispatch_stage.py` | `prema.dispatch.stage` | Pipeline stage config (`stage_type`, `is_booking_phase` etc.); 16 stages seeded in `data/dispatch_stage_data.xml`. |
| `dispatch_assignment_log.py` | `prema.dispatch.assignment.log` | Auto-logged truck/driver reassignment history (write() hook in `dispatch_job.py`). |
| `dispatch_location.py` | `prema.dispatch.location` | Saved pickup/delivery locations: precise parking pin, entrance photo, dock/equipment flags, visit stats. |
| `dispatch_crossdock.py` | `prema.dispatch.crossdock.location`, `prema.dispatch.custody.event` | Cross-dock hub locations + custody chain-of-event log. |
| `dispatch_consolidation.py` | `prema.dispatch.consolidation` | LTL consolidation *suggestions* (persistent but not a "run" aggregate — confirmed no execution-level model exists besides Load Plan). |
| `dispatch_consolidation_wizard.py` | `.consolidation.line`, `.consolidation.wizard` | UI wizard for accepting a suggested consolidated route. |
| `dispatch_adhoc_wizard.py` | `.adhoc.wizard`, `.adhoc.result` | "Find Available Truck" mid-day load finder wizard. |
| `dispatch_feasibility.py` | `.feasibility.wizard` | "Can we do this today?" real-time feasibility check wizard. |
| `dispatch_chat_invite_wizard.py` | `.chat.invite.wizard` | Add/remove members on a driver↔dispatch chat channel. |
| `dispatch_timeline.py` | `.timeline.event` | Full event-history timeline per job (separate from Load Plan's own event log). |
| `booking_template.py` | `.booking.template` | Historical compatibility model only; active recurring work uses `logistics.recurring.agreement`. |
| `dispatch_reports.py` | 9 report wizard models + `.driver.worksheet` | All reporting-suite wizards (On-Time %, Stop-Time-by-Location, Performance, Lane Profitability, Fuel Efficiency, Late-Stop, POD Aging) + the live/historical Driver Worksheet. |
| `account_move_dispatch.py` | *(inherits `account.move`)* | Invoice-side dispatch integration (Book Load button, AI extraction hookup). |
| `sale_order_dispatch.py` | *(inherits `sale.order`)* | Sales-order-side dispatch integration. |
| `res_partner_dispatch.py` | *(inherits `res.partner`)* | `x_is_driver`, `action_create_driver_account()` (driver onboarding: grants `base.group_user` + `group_dispatch_driver` together — never driver-group alone). |
| `voip_call_extension.py` | *(inherits `voip.call`)* | Dispatcher-wide call visibility rule support. |
| **`dispatch_load_plan.py`** | `prema.dispatch.load.plan`, `.load.plan.job`, + a `prema.dispatch.job` `_inherit` (auto-lock hook only) | **Load Plan core** — one physical vehicle-loading execution, may span several financially-separate jobs. All CRUD/mutation RPC methods live here: `create_load_plan`, `get_or_create_for_vehicle_date(_warehouse)`, `assign/move/swap/unassign_pallet`, `assign_stops_to_pallet`, `change_layout`, `evaluate_layout_for_capacity`, `validate_load_plan`, `confirm_loading`, `lock_load_plan`/`unlock_load_plan`, `acknowledge_unverified_layout`, `execute_handoff`, `report_exception`, `upload_document`. |
| **`dispatch_vehicle_layout.py`** | `.vehicle.layout.template`, `.vehicle.layout.position` | Configurable truck layout templates (Straight/Pin-Wheel/Turned) + individual floor positions. Seeded unverified in `data/dispatch_layout_template_data.xml`. |
| **`dispatch_pallet_allocation.py`** | `.pallet.stop.allocation` | Many-to-many join: one physical pallet → many delivery stops (shared skids). `unique(dispatch_item_id, stop_id)`. |
| **`dispatch_load_plan_event.py`** | `.load.plan.event` | Load Plan audit log (created/assigned/moved/locked/handed_off/etc. — see `EVENT_TYPES` list in file). |
| **`dispatch_document.py`** | `.document` | Thin metadata wrapper around `ir.attachment` for Load Plan documents (route_sheet/pod/pop/damage_photo/etc.) — does not duplicate binary storage. |
| **`dispatch_book_load_wizard.py`** | `.book.load.wizard` (Transient) | Canonical Invoice Book Load wizard: Scheduled Network uses Corridor pricing/exact departures; Custom/Expedited uses an explicit agreed rate. Reuses the existing draft invoice and never falls back to direct dispatch. |
| **`dispatch_location_photo.py`** | `.location.photo` | Photo history per Saved Location (entrance/dock/parking/etc.), separate from the location's single legacy `entrance_photo` field. Read via `dispatch_location.py`'s `_driver_payload()`. |
| **`dispatch_location_extraction.py`** | `.location.extraction` | Audit trail for AI photo→location extraction calls (Ship To vs Invoice To), keyed by image SHA-256 so a re-scanned photo doesn't re-call the AI provider. Populated by `services/location_extraction_service.py`. |
| **`dispatch_route_visit.py`** | `.route.visit`, `.route.visit.stop` | Combines 2+ delivery stops (from *different, financially separate* jobs on the same Load Plan) that share one physical address into one visit/map-marker/arrival-event, while each stop keeps its own job/invoice/completion state. Created via `dispatch_load_plan.py`'s `combine_physical_visit()`. |
| **`dispatch_load_plan_operation.py`** | `.load.plan.operation` | Exact position-level operation log: `reserve_position` (future pickup), `temporary_unload`/`reload` (rehandle steps), etc. Generated by `dispatch_load_plan.py`'s `reserve_future_positions()`/`get_future_pickup_plan()`. |

**`dispatch_job.py` additions (2026-07-20):** `route_definition_mode` (`exact_stops`/`stops_pending`), `stops_confirmation_state`, `planned_route_name`/`planned_route_corridor` (EAST/WEST/NORTH/SOUTH/LOCAL/CUSTOM) vs. computed `computed_route_corridor`/`effective_route_corridor` (derived from `delivery_cities` keyword matching) + `corridor_mismatch_warning`, `pickup_saved_location_id`, `route_sheet_received_at/_by`, `_driver_job_summary()`.

**`dispatch_item.py` addition:** `available_after_stop_id` + computed `pending_future_pickup` — an item tied to a not-yet-departed pickup stop is excluded from `confirmed`/`assigned`/`loaded` Load Plan counts until that stop's `actual_departure_time` is set (used for second-pickup-on-the-same-route scenarios so the freight never shows as onboard before it physically is).

**`dispatch_location.py` additions:** `chain_name`/`location_number` (+ normalized/search-key computed fields, unique-per-chain-per-number constraint), `verification_state`/`source_type`/`pin_source`/`pin_accuracy_m`, `driver_search_locations()` (chain+store-number search, e.g. "Foodland 3290"/"Foodland #3290"; falls back to an all-query-words-present match on `location_search_key` for natural-language queries like "No Frills Belleville" that aren't a contiguous substring of the business name).

**`dispatch_location.py` additions (2026-08-08):** `stop_type` editable field (pickup/delivery/both — "Pickup & Delivery") — controls which booking selectors this location appears in. `usage_type` computed field (pickup/delivery/both/unknown — "Historical Usage") — derived from `stop_ids.stop_type` history. Three separate concepts: Location Type (physical facility), Stop Type (allowed dispatch usage, editable), Historical Usage (computed from actual stops, read-only). Surfaced in form/list/search views, driver app search results, and `_driver_payload()`. Portal sync in `logistics_saved_location.py` maps customer `location_type` → dispatch `stop_type` for new locations, never overwrites linked master facilities. **`name_get()`** now uses `location_display_label` (Business — City) instead of raw address fallback.

**Portal saved-location form (2026-08-08):** Province dropdown auto-fill fixed — added `data-code` attributes to `<option>` elements and `setProvinceByCode()` helper that matches by code (ON) first, name (Ontario) second. Works for both Google Places autocomplete and facility-name suggestion prefill (previously only worked for shared facilities). Simple form province dropdown fixed too (was empty).

**UAT-004 — Master Stop Type → Portal Location Type (2026-08-08):** Autocomplete endpoint `_format_dispatch_result()` was hardcoding `location_type: "pickup"` for all shared facilities — now returns the master's actual `stop_type`. `prefillFromSuggestion()` JS now updates the Location Type dropdown from the autocomplete result. **Precedence:** master facility `stop_type` > saved location's own type > URL `?type=` parameter. Master data never overwritten by customer preference; customer can still override their own saved location type independently.

**UAT-005 — Booking flow HTTP 500 fix (2026-08-09):** `booking_step2` Route A (saved locations) was rendering `portal_step2_shipment` without `pickup_fsa`/`delivery_fsa` — template crashed on `KeyError: 'pickup_fsa'`. **Fixed:** (1) controller now resolves FSA from saved location postal codes, falls back to code-search then RegionResolver; passes both `pickup_loc`/`delivery_loc` and `pickup_fsa`/`delivery_fsa` to template. (2) template rewritten to show saved-location cards when available, FSA display otherwise; preserves `pickup_loc_id`/`delivery_loc_id` through hidden fields. (3) `booking_quote` controller resolves FSA from saved location IDs first, falling back to FSA codes. (4) Step 1 "Default: X" text replaced with dynamic JS showing selected location details.

**UAT-006 — Multi-corridor portal quote routing (2026-08-09):** Portal `prepare_quote` was using legacy `pricing.calculate()` which only does direct corridor lookup → `no_corridor_for_regions` for R-GTA→R-SEO. **Fixed:** (1) `prepare_quote` now routes through canonical `ShipmentRoutingService.plan_route()` when coordinates available (saved location mode), falling back to legacy pricing for FSA-only mode. (2) `booking_quote` passes coordinates from saved locations + hidden form fields into `pickup_stops`/`delivery_stops`. (3) Step 2 template: added `requested_pickup_date` (date picker, defaults to tomorrow, min=today), restored 500lb default weight. (4) Error messages map technical reason codes to customer-friendly text; `no_corridor_for_regions` never exposed. (5) Hub transfer routing confirmed: R-GTA→Hub (LOCAL corridor Mon/Thu) + Hub→R-SEO (GTA→QUEBEC Tue or GTA→OTTAWA Fri), $150 min applied once.

**UAT-007 — Smart pickup calendar + pallet weight default (2026-08-09):** (1) Added `get_eligible_pickup_dates()` to `ShipmentRoutingService` — probes all dates in 8-week horizon, returns only those with feasible legs/corridors/departures. (2) Added `/my/booking/eligible-dates` JSON endpoint. (3) Replaced generic `<input type="date">` with visual 4-week calendar grid showing available dates (red dots), unavailable dates (grey), past dates (disabled). (4) Auto-selects earliest eligible date. (5) Pallet-weight auto-calculation: `pallets × 500` lb with manual-override detection and reset link.

**UAT-008 — Redesigned pickup date selector (2026-08-09):** (1) Primary UI: 4 large date cards (MON AUG 10, THU AUG 13, ...) with DAY/DATE/Regular Pickup label, responsive (col-md-3 desktop, col-6 mobile). Selected card: Premafirm red (#C62828) background, white text, "✓ SELECTED" badge. (2) Selected date display: "Monday, August 10, 2026" in Premafirm blue above cards. (3) "View More Pickup Dates" toggles proper monthly calendar with month navigation, SUN-SAT headers aligned to dates, selected=red circle, available=red dot. (4) Single `selectDate()` source-of-truth synchronizes cards, calendar, hidden field, and display label. (5) Clean legend, "Service Options" heading, larger Get Price button. Backend routing unchanged.

**UAT-009 — Customer price/review page redesign (2026-08-09):** (1) Step 3 template completely redesigned: clean "Review & Book" page with rate card (pickup/estimated delivery date, service label), shipment details card (pallets/weight/type), "What You Are Booking" card (read-only saved-location addresses), pricing summary card (single line total, no leg breakdown). (2) `logistics.pricing.session` now stores `pickup_saved_location_id`/`delivery_saved_location_id` — frozen from Step 1, used for Step 3 display and Step 4 confirm. (3) Confirm controller pulls address data from session's frozen saved locations instead of requiring re-entry. Only contact name, phone, and instructions remain editable. (4) Internal corridor names, leg breakdowns, and individual leg prices never exposed to customers.

**UAT-010 — Multi-stop delivery bookings (2026-08-09):** (1) New `logistics.pricing.session.stop` transient model for per-delivery-stop data (sequence, saved_location_id, address snapshot, pallets, weight, accessorials). (2) Step 1: dynamic multi-stop delivery UI with add/remove/reorder buttons, indexed form fields (`delivery_saved_location_id_1`, `delivery_saved_location_id_2`, ...), max 20 stops enforced in JS + server. (3) Step 1 controller collects all delivery stop IDs from indexed form fields. (4) Step 2 controller resolves all delivery locations, passes `delivery_locs` list to template. (5) `prepare_quote` creates per-stop session records with individual pallets/weight. (6) Step 3 template shows all delivery stops with per-stop pallets/weight in "What You Are Booking" card. Total pallets/weight auto-calculated from stop-level data.

**UAT-011 — Per-stop Contact & Instructions (2026-08-09):** (1) Generic "CONTACT & INSTRUCTIONS" card removed from Step 3. (2) Each pickup and delivery stop now has its own collapsible "CONTACT & INSTRUCTIONS ▾" section directly under its address card. (3) Fields auto-populate from saved location: contact_name, contact_phone, dock_info, pickup/delivery_instructions. (4) Changes are shipment-specific overrides — they don't modify the saved location master. (5) Per-stop fields submitted with indexed names (`delivery_contact_name_1`, `delivery_phone_1`, `delivery_instructions_1`, etc.). (6) Confirm controller collects per-stop data and passes `delivery_stops_data` to booking creation. (7) JS toggle: clicking one stop's panel doesn't affect others, arrow indicator changes ▾↔▴.

**UAT-012 — Contact Architecture + Driver Instructions (2026-08-09):** (1) Prod-db backed up (26MB). (2) "Structured Driver Guidance" removed from form view, consolidated under "Driver Instructions". (3) `parking_notes` migrated to `driver_instructions` (1 record). (4) **Architecture correction**: master facility (`prema.dispatch.location`) = physical only (address, dock door, gate code, receiving/truck entrance, universal facility access). Customer saved location (`logistics.saved.location`) = customer-specific profile (contact name/phone/email, dock info, pickup/delivery instructions, driver instructions). (5) `_format_dispatch_result` shares physical access info only (dock_door, gate_code, receiving_entrance, truck_entrance, driver_instructions as facility access) — never shares contact fields. (6) `_sync_dispatch_location` no longer writes customer instructions to master — only updates partner link. (7) `_format_result` (customer's own locations) includes all contact fields for autocomplete prefill. (8) Test contacts on saved locations (not masters): United Dairy→Mike Johnson, Healthy Planet→John Smith. (9) Multiple customers can have separate profiles for the same master facility with different contacts.

**UAT-013 — Booking confirmation rejects valid corridor quote (2026-08-09):** `confirm_from_session()` checks `route_snapshot.pricing_authority == "corridor_per_km"` to reject legacy quotes. `ShipmentRoutingService.plan_route()` was not setting this field in its snapshot → all new corridor-based quotes were rejected as "retired pricing setup." **Fixed:** added `"pricing_authority": "corridor_per_km"` and `"pricing_version": "current"` to `plan_route()`'s snapshot dict. Legacy quotes without corridor_per_km authority are still correctly rejected.

**`dispatch_load_plan.py` additions:** `find_shared_visit_candidates()`/`combine_physical_visit()` (Phase 20 shared-address handling), `reserve_future_positions()`/`get_future_pickup_plan()`/`confirm_future_pickup_operation()` (future-pickup reservation + exact rehandle instructions, preferring positions nearest the door via `distance_from_rear_in`), `reserved_pallet_count`/`committed_pallet_count`/`available_position_count` (commitment = `max(reserved, confirmed)` per job, never `reserved + confirmed`).

## Controllers (`controllers/`)

| File | Routes | Purpose |
|---|---|---|
| `driver_app.py` | `/dispatch/driver*` (page + ~20 JSON RPC routes) | Driver App backend — schedule, stops, evidence upload, chat, pin editing. Every mutating route delegates to `driver_*` methods on `dispatch_job.py`, which run through `services/dispatch_auth.py` checks. |
| `load_plan_driver.py` | `/dispatch/driver/loadplan/*` | Driver-facing Load Plan JSON routes — thin wrappers around `dispatch_load_plan.py` methods, catch exceptions into `{success:false, error:...}` (never raise to the browser). |
| `warehouse_app.py` | `/dispatch/warehouse*`, `/dispatch/pallet/<token>` | Warehouse Loader page + JSON routes (reuse the same Load Plan model methods, warehouse-aware via `dispatch_auth.py`). **Also hosts the public QR route** — read-only, no financial data, `auth="public"`. |
| `portal.py` | `/dispatch/track/<tracking_number>*`, `/web/dispatch/live-map/data` | Public customer shipment tracking page + live map data feed. |
| `manual.py` | `/dispatch/manual` | Serves the in-app user manual (`views/dispatch_manual_template.xml`). |
| `call_recording.py` | `/prema_dispatch/call_recording/<id>` | Best-effort Asterisk call-recording playback (GSM→MP3 transcode). |
| `driver_app.py` additions (2026-07-20) | `/dispatch/driver/job/summaries`, `/route-sheet-received`, `/location/search`, `/location/get`, `/location/duplicates`, `/location/create`, `/location/extract`, `/location/photo/upload`, `/stop/create` | Stops Pending workflow + manual driver location creation (with duplicate-detection) + Ship-To photo extraction + location photo history. All gate through `services/dispatch_auth.py`'s `check_driver_can_add_stop`/`check_driver_can_create_location`. |

## Services (`services/`) — class-based, `__init__(self, env)`, stateless business logic

| File | Class | Purpose |
|---|---|---|
| `feasibility_service.py` | `DispatchFeasibilityService` | "Can we pick up X and deliver by Y?" real-time check across all trucks. |
| `availability_service.py` | `DispatchAvailabilityService` | `get_truck_day_schedule()` — live per-truck/day job+stop aggregation (no persisted "run" model exists; this is why Load Plan was built as the first persisted vehicle+date aggregate). |
| `optimization_service.py` | `DispatchOptimizationService` | Stop-sequence optimization (nearest-neighbor + urgent-deadline priority). |
| `route_service.py` | `DispatchRouteService` | Google Directions-backed ETA/route estimation for a job. |
| `adhoc_load_service.py` | `AdhocLoadService` | Mid-day "Find Available Truck" candidate scoring + Distance Matrix refinement. |
| `dispatch_auth.py` | `DispatchAuthService` (+ module-level fn wrappers) | **Authorization helpers** — `check_job_access`/`check_stop_access`/`check_item_access`/`check_load_plan_access`, `is_dispatch_staff`/`is_warehouse_user`. Fixed a real cross-driver IDOR here (Phase 1A) — every driver-facing mutating method must call one of these first. |
| `dispatch_upload.py` | `UploadError` + functions `decode_and_validate`/`find_duplicate`/`sanitize_filename` | **Shared upload validator** — real content-signature detection (JPEG/PNG via Pillow, PDF/HEIC by signature), 15MB cap, filename sanitization, per-record SHA-256 dedup. Reused by both Driver App evidence uploads and Load Plan document uploads — do not build a second validator. |
| `dispatch_recommendation_service.py` | `DispatchRecommendationService` | Rule-based (not ML) pallet-position recommendations — earliest-delivery-nearest-rear ordering, four-way/weight/side-balance warnings. Always advisory. |
| `location_extraction_service.py` (2026-07-20) | `LocationExtractionService` | Ship-To-vs-Invoice-To photo extraction. Reuses the existing `openai_utils.openai_chat` vision helper from `premafirm_ai_engine` (no separate/hardcoded API key) with an explicit "ignore Invoice To" system prompt; validates the response against a strict key whitelist; SHA-256-deduped via `prema.dispatch.location.extraction` so a re-scanned photo doesn't re-call the provider. Never auto-saves a location — the driver must confirm. |

## Frontend (`static/src/`)

| Feature | JS | XML | CSS |
|---|---|---|---|
| Live Map | `live_map.js` | `live_map.xml` | `live_map.css` |
| Dispatch Planner (Booking Board / truck board) | `dispatch_board.js` | `dispatch_board.xml` | `dispatch_board.css` |
| Booking Board (status overview) | `booking_status_board.js` | `booking_status_board.xml` | `booking_status_board.css` |
| **Pallet Layout panel** (dispatcher, mounted inside Planner) | `pallet_layout.js` (OWL `PalletLayoutPanel`) | `pallet_layout.xml` | `pallet_layout.css` |
| Driver App (standalone page, **not** in the OWL asset bundle — loaded via `<script>` tag) | `driver_app.js` | *(template is `views/driver_app_template.xml`)* | `driver_app.css` |
| Warehouse App (standalone page, same pattern as Driver App) | `warehouse_app.js` | *(template is `views/warehouse_app_template.xml`)* | `warehouse_app.css` |
| Misc | `dispatch_time_utils.js` (shared tz/12h formatter for OWL), `google_places_widget.js` (address autocomplete, no `types` restriction) | — | — |
| Vendored libs | `lib/jscanify.min.js` (document scanner, used by Driver App's Scan Doc) | — | `lib/leaflet/` (unused legacy?) |

**Driver App navigation model (`driver_app.js`):** plain `showScreen()` display-toggle across `sSchedule/sStop/sNav/sLoadPlan` (no OWL, no framework router) — extended in Phase 1B with History API (`pushState`/`popState`) for refresh/back-safety. Do not add a second router; add new screens to the same array.

**Upload state machine (`driver_app.js`):** `pickEvidenceFile → runEvidenceUpload` — idle/selected/preparing/uploading/success/duplicate/failed, via `rpcWithProgress()` (XHR, for real transmission progress; `rpc()` stays fetch-based for everything else).

## Views (`views/`)

Mostly one file per model group, named `<model>_views.xml`. Notable:
- `menus.xml` — full menu tree; **must load before** any file whose menuitems reference its parent ids (`dispatch_load_plan_views.xml` is deliberately listed *after* `menus.xml` in the manifest for this reason).
- `driver_app_template.xml` / `warehouse_app_template.xml` — standalone page templates (`t-call="web.layout"`, own `<link>`/`<script>` tags, not the backend asset bundle).
- `dispatch_manual_template.xml` — in-app user manual, section 9 covers Load Plan/pallet positioning/warehouse/QR.

## Security (`security/`)

- `dispatch_security.xml` — groups: `group_dispatch_manager`, `group_dispatcher`, `group_dispatch_readonly`, `group_dispatch_driver`, `group_dispatch_warehouse`. `ir.rule`: driver scoped to own jobs/stops/load-plans (defense-in-depth; the real enforcement is `dispatch_auth.py` checks inside each method). Warehouse is scoped by *operational state* in code, not an identity-based rule (many warehouse workers load many trucks).
- `ir.model.access.csv` — one row per model × group. **Gotcha already hit once:** adding a new model here is easy to forget for a *specific* group (driver/warehouse access to `prema.dispatch.item` was missing until caught by tests) — always add rows for manager/dispatcher/driver/warehouse together, not just manager/dispatcher.

## Data (`data/`)

- `dispatch_stage_data.xml` — 16 seeded stages.
- `dispatch_cron.xml` — recurring booking-template cron.
- `dispatch_layout_template_data.xml` — 3 seeded 26ft templates (Straight/12, Pin-Wheel/13, Turned/14), all `is_verified=False` until real measurements are entered (see the open admin task "VERIFY 26-FOOT TRUCK LAYOUT MEASUREMENTS").

## Tests (`tests/`)

| File | Covers |
|---|---|
| `test_dispatch.py` | Original 28+ capacity/timezone/assignment/cross-dock/custody tests (pre-Load-Plan). |
| `test_driver_authorization.py` | Cross-driver IDOR regression (Phase 1A). |
| `test_upload_validation.py` | Upload signature/size/dedup validation + evidence-upload integration (Phase 1C). |
| `test_load_plan.py` | Load Plan model/capacity/shared-skid/concurrency/stale/lock/transfer/warehouse/QR/unverified-layout-acknowledgement tests (Phase 2-7 + production safety patch). |

Run: `./odoo-bin -c odoo18.conf -d <test-db> --test-enable --test-tags /prema_dispatch -u prema_dispatch`. **3 pre-existing failures are expected and unrelated** — they're blocked outbound Google Maps API calls in the test sandbox (`test_24_cross_dock_interleave_avoids_false_infeasibility`, `test_autoplan_dispatcher_sequence_00113_00114_pattern`, `test_planner_payload_shows_stop_action_labels`). Anything beyond those 3 is a real regression.

## Key architectural facts worth knowing before editing

- **Timezone:** server clock is UTC, business is Toronto — always use `_user_today(user_tz)` / the tz-aware helpers already in `dispatch_job.py`, never bare `date.today()`.
- **Driver identity precedence:** always `driver_id` (Fleet's assigned driver) over `x_current_driver_contact_id` (GeoTab live telemetry) — the latter is often stale.
- **Transfer/custody segments** (`_job_segments()` in `dispatch_job.py`) are computed live, never persisted — Load Plan's `origin_stop_id` field is the closest thing to a persisted segment pointer.
- **`prema.dispatch.item` has no `active` field** — use `status != 'cancelled'` for "is this item still relevant," not `.filtered("active")`.
- **Odoo shell gotcha:** `odoo-bin shell` does NOT auto-commit — every real data change needs an explicit `env.cr.commit()`.
- **Deployment gotcha:** always `systemctl restart odoo18` after `-u`, not just the offline upgrade — multi-worker registries go stale otherwise.

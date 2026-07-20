# Prema Dispatch — File Index

Purpose: let a future session find the right file in one lookup instead of
grepping/exploring. Keep this updated when files are added/removed/renamed.
Module: `prema_dispatch` · Path: `/opt/odoo/custom-addons/prema_dispatch` ·
DB: `Prod-db` · Config: `/etc/odoo18.conf` · Version: `18.0.2.0.0`

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
| `dispatch_duplicate_job_wizard.py` | `.duplicate.job.wizard` | "Job already exists" dedup prompt when Book Load is clicked twice. |
| `dispatch_chat_invite_wizard.py` | `.chat.invite.wizard` | Add/remove members on a driver↔dispatch chat channel. |
| `dispatch_timeline.py` | `.timeline.event` | Full event-history timeline per job (separate from Load Plan's own event log). |
| `booking_template.py` | `.booking.template` | Recurring booking templates + daily cron to auto-create bookings. |
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

## Controllers (`controllers/`)

| File | Routes | Purpose |
|---|---|---|
| `driver_app.py` | `/dispatch/driver*` (page + ~20 JSON RPC routes) | Driver App backend — schedule, stops, evidence upload, chat, pin editing. Every mutating route delegates to `driver_*` methods on `dispatch_job.py`, which run through `services/dispatch_auth.py` checks. |
| `load_plan_driver.py` | `/dispatch/driver/loadplan/*` | Driver-facing Load Plan JSON routes — thin wrappers around `dispatch_load_plan.py` methods, catch exceptions into `{success:false, error:...}` (never raise to the browser). |
| `warehouse_app.py` | `/dispatch/warehouse*`, `/dispatch/pallet/<token>` | Warehouse Loader page + JSON routes (reuse the same Load Plan model methods, warehouse-aware via `dispatch_auth.py`). **Also hosts the public QR route** — read-only, no financial data, `auth="public"`. |
| `portal.py` | `/dispatch/track/<tracking_number>*`, `/web/dispatch/live-map/data` | Public customer shipment tracking page + live map data feed. |
| `manual.py` | `/dispatch/manual` | Serves the in-app user manual (`views/dispatch_manual_template.xml`). |
| `call_recording.py` | `/prema_dispatch/call_recording/<id>` | Best-effort Asterisk call-recording playback (GSM→MP3 transcode). |

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

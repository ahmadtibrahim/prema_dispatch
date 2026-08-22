# Driver Flow V7 — UAT Report (guided flow + mutation-storm fix)

- **Branch**: `feature/driver-guided-flow-v7` (PR #7 — **NOT merged, NOT deployed**)
- **Environment**: Prod-db-uat on `127.0.0.1:8075` (never Prod-db / 8069)
- **Date**: 2026-08-19 → 2026-08-20 (walkthrough completed 08-20 03:16 UTC)
- **Driver fixture**: `uat.driver@premafirm.com` (uid 502, partner 2118, groups: Internal User + Dispatch/Driver)
- **Route under test**: DISP/2026/00448 + DISP/2026/00449 (8 stops; United Dairy pickup pre-`arrived`, the exact state that previously stormed)

## MUTATION STORM — root cause, fix, and verification

### Symptom (UAT session before the fix)

On the arrived/pickup screen the browser became progressively unusable:
main-thread latency escalated **~5.5s → ~9.4s → ~90.7s** over a few minutes,
with page interaction freezing and the MutationObserver callback queue
growing without bound.

### Root cause

Three render sites on the driver page performed **unconditional DOM writes on
every audit pass**, and the observer **queued one `requestAnimationFrame` per
mutation record** — so every write re-triggered the observer, and every frame
could carry N new audit jobs (N×N blowup per frame):

1. `renderSimplifiedStop()` — unconditional `body.innerHTML = html`, worst in
   the Arrived branch (whole stop card re-created each pass).
2. `renderGuide()` — `v7GuideProgress` / `v7GuideBody` re-rendered on every
   audit pass while the guide was open, even with zero state change.
3. `postProcessLoadPlan()` — `.da-lp-unverified-banner` `innerHTML` rewritten
   every pass even when already correct.
4. Contributing: `enforcePickupActionOrder()` (v6) re-`appendChild`-ed action
   buttons every pass (latent reorder loop), and unguarded
   `postProcessList()` / nav-tab / badge `textContent` writes.

### Fix (commit `9075052` on the feature branch; also `662deb4`)

| File | Change |
|---|---|
| `static/src/js/driver_guided_flow_v7.js` | `renderSimplifiedStop()`: stable render key (`JSON.stringify` of stop id/type/status/arrival/summary/expected/deferred/exception/required-action state) stamped in `dataset.v7RenderKey`; **no write when unchanged** (self-heals if the base app overwrites the card). `renderGuide()`: content signature (stop id, mode, step, unload-confirmed, load-plan state, pending-evidence state) — writes only when the signature changes; driving-mode lock applied outside the signature so it still responds immediately. `postProcessLoadPlan()`: compare-then-write. `postProcessList()`: guarded badges + one-time `onclick` binding. `auditDom()`: guarded writes. |
| `static/src/js/driver_flow_v6.js` | `enforcePickupActionOrder()`: reorder only when the DOM order actually differs (`needsFix`); boot observer coalesced with the same queued-audit flag. |
| `tests/test_driver_guided_flow_v7.py` | `TestDriverGuidedFlowV7MutationStorm` — 7 static contracts pinning coalescing (one audit per frame) and idempotency (single innerHTML behind render-key/signature guards, compare-then-write, one-time bindings). |

**Prevention**: every render path now compares desired vs current state before
mutating; the observer coalesces into a single rAF audit; regression tests
fail red if a refactor reintroduces per-record scheduling or unconditional
writes. No observer disabling, no `setTimeout` hacks, no guided-flow behavior
removed, no error suppression.

### Verification (headless Chromium 390×844, Prod-db-uat, 2026-08-19)

Instrumentation: a second MutationObserver counting records on `#app`
(subtree) + rAF-gap latency sampler + `window.__v7Perf` render counters
(written only by the fixed code).

| Phase | Mutations | audits | stopR | guideR | lpW | lat max | lat avg | bad>100ms |
|---|---|---|---|---|---|---|---|---|
| **BEFORE fix** (arrived pickup, ~3 min) | unbounded growth | — | — | — | — | **90,700ms** | 5.5s→9.4s→90.7s | all frames |
| AFTER — baseline schedule (40s idle) | 9 | 7 | 0 | 0 | 0 | 102ms | 16.8ms | 2 |
| AFTER — stop detail (arrived) | 430 | 21 | 1 | 0 | 0 | 102ms | 16.9ms | 2 |
| AFTER — guide open (Step 1 of 6) | 1421 | 30 | 1 | 1 | 0 | 1418ms | 17.9ms | 10 |
| AFTER — guide idle 60s | 1421 | 30 | 1 | 1 | 0 | 1418ms | 16.7ms | 10 |
| AFTER — guide idle 120s | *flat* | *flat* | 1 | 1 | 0 | *flat* | *flat* | *flat* |
| AFTER — guide idle 180s | *flat* | *flat* | 1 | 1 | 0 | *flat* | *flat* | *flat* |
| AFTER — load plan open | not measured on 08-19 → measured on the 02:11 re-verify (below) | | | | | | | |
| **RE-VERIFY 2026-08-20 02:11 UTC** (post-fix re-run, same arrived pickup) | | | | | | | | |
| baseline schedule (40s idle) | 0 | 6 | 0 | 0 | 0 | 58.1ms | 16.7ms | 0 |
| stop detail (arrived) | 11,727 | 60 | 1 | 0 | 0 | 86.1ms | 16.7ms | 0 |
| guide open (Step 1 of 6) | 11,733 | 61 | 1 | 1 | 0 | 86.1ms | 16.7ms | 0 |
| guide idle 30s … 180s (6 windows) | **11,733 flat** | **61 flat** | 1 | 1 | 0 | 86.1ms | 16.7ms | **0** |
| load plan open | 11,742 | 64 | 1 | 1 | **1** | 86.1ms | 16.7ms | 0 |

**Result: fully resolved.** With the guide open on the arrived pickup — the
exact 90.7s scenario — the DOM is completely silent while idle: zero
mutations, zero renders, flat audit count. All render counters moved exactly
once per real interaction (1 stop render on open, 1 guide render on open, 0
load-plan writes). The remaining latency spikes (102ms initial load, 1.4s at
guide-open) are one-time layout/font/load-plan-fetch cost, not an escalating
loop: the bad-gap count did not grow during the idle window.

The 2026-08-20 02:11 re-verification (commit `f87a654` state, headless
Chromium 390×844) confirms it on a second run: **11,733 mutations total and
61 audits, completely flat across six 30s idle windows**, `latBad` 0, and the
only counter that moved during idle was nothing. The load-plan open is now a
measured row: exactly **one** load-plan write (`lpW=1`), 9 mutations, 3 audits
— the single write the interaction itself causes, with zero echo. (Absolute
mutation counts are higher than the 08-19 run because this page session had
longer history — stop rows re-rendered by the base app during earlier
navigations; what matters is *flatness while idle* and *counter moves exactly
once per real interaction*.)

Full walkthrough results: see below.

## UAT environment findings (not code defects)

1. **`prema.dispatch.stage` AccessError on first login** — a fixture mismatch,
   not a v7 regression: prod driver accounts are created by
   `action_create_driver_account` as **Internal User + Driver** (base.group_user
   is explicitly added "required for app access"), so drivers pass the stage
   read ACL (`base.group_user` read row). The UAT user had been created with
   the Driver group only. Fixed on Prod-db-uat (groups 1 + 135, `share=false`),
   then restarted the UAT workers (direct-SQL group changes bypass Odoo's
   `res.groups` ormcache). The app then loaded the full route.
2. **`window.S` is module-scoped** (`const S` in `driver_app.js`) — it never
   existed on `window`; the earlier "S never populates" signal was a bad
   probe. Route-ready is `#sLoading` hidden.
3. **WebSocket 500 on `ws://127.0.0.1:8075/websocket`** — environment noise
   (frontend defaults the WS port to the page port; the evented port is
   8078). Pre-existing on this box, does not affect the standalone driver
   app (HTTP polling + bus RPC work).

## Defect found in walkthrough: ISO-8601 `captured_at` blocked the pallet-photo gate

### Symptom

Phase B walked the guided pickup through steps 1–3 (pallet count, destinations,
positions) but stalled at step 4: all four pallet photos uploaded, yet the guide
never advanced to step 5 (`wait_step(5)` timed out at 45s). The server had in
fact committed the evidence rows and item↔attachment links — but every upload
response was `success=False`, so the client's gate state never recorded the
photos and "continue" stayed disabled.

### Root cause (two defects)

1. **`_create_evidence` crashed on the app's timestamp.** The driver app sends
   `captured_at` as `new Date().toISOString()` (e.g. `2026-08-20T00:53:35.722Z`).
   The ORM `Datetime` field only accepts `YYYY-MM-DD HH:MM:SS`; the conversion
   raised `ValueError`, the generic exception handler returned `success=False`
   (the evidence row still committed), and the client treated the upload as
   failed.
2. **The controller dropped `extra`.** `/dispatch/driver/evidence/add` forwarded
   `extra` into `**kwargs`, so the model's popp branch never received
   `extra['pallet_id']` and every pallet-photo upload would be rejected with
   `pallet_not_found` before reaching the gate. (Pre-existing evidence tests
   called the model directly, so the route regressed silently.)

### Fix (uncommitted on the feature branch; this session)

| File | Change |
|---|---|
| `models/dispatch_evidence.py` | `_create_evidence` normalizes ISO-8601 `captured_at` (`T`→space, strip fractional seconds and `Z`) before the ORM write |
| `controllers/driver_app.py` | `add_evidence(...)` now accepts `extra=None` explicitly and forwards `extra=extra` to the model |
| `tests/test_evidence_workflow.py` | `test_01b_iso8601_captured_at_accepted` (pins the normalization end-to-end) + `TestEvidenceControllerForwardsExtra` (static contracts: route signature accepts `extra`, route forwards it) |

### Verification

Evidence suite green; full suite green (331/331). Walkthrough re-run (below):
step-4 uploads accepted (`success=True`), gate advanced to step 5, pickup
completed through step 6.

## Defect found in walkthrough: defer 500'd on an email-less driver and the app failed silently

### Symptom

Phase B's `Come Back Later` step (2026-08-20 02:16 UTC): the defer on the
NOFRILLS stop 1047 **failed silently** — the stop stayed `en_route`, no toast,
no badge, and the `Return to This Stop` button never rendered (the walkthrough
then hung on a no-op click). The server log showed the defer RPC took 26ms and
returned a JSON-RPC error:

```
odoo.exceptions.UserError: Unable to send message, please configure the
sender's email address.
```

### Root cause (three layers)

1. **Server crash inside the guided handler.** `_post_driver_audit()`
   (`models/driver_guided_flow.py`) calls `job.message_post(...)` with the
   driver as author. Odoo 17+ stores the email on `res.partner` (not
   `res_users`), and `message_post` raises `UserError` when the author
   partner has no email. The audit note is written *after* the stop's state
   change inside the same transaction — the raise **rolled back the defer
   itself**. Production driver accounts are always created with
   `login=partner.email` (`action_create_driver_account`), so they always
   have emails; this hits manually-created accounts — and the UAT fixture,
   which is how the walkthrough found it.
2. **The client swallowed the failure.** The five guided transitions
   (`defer`, `resume_deferred`, `report_problem`, `resume_exception`,
   `make_next`) called the JSON-RPC `rpc()` helper *bare*. `rpc()` throws on
   a JSON-RPC error, so the server 500 became an unhandled promise rejection:
   zero user feedback, the stop card just never changed.
3. **Fixture gap.** Partner 2118 (the UAT driver) had no email, so the UAT
   exercised a shape prod accounts never have.

### Fix

| File | Change |
|---|---|
| `models/driver_guided_flow.py` | `_post_driver_audit()` is now **best-effort**: `try/except` around `message_post` with a `_logger.warning` — an audit note can never roll back the driver action it accompanies |
| `static/src/js/driver_guided_flow_v7.js` | new `guidedStatusCall()` helper wraps `rpc()` in `try/catch` and toasts `"<failMsg>: <server error>"` on failure; all five guided call sites route through it (return early when the call failed or `success=false`) |
| `tests/test_driver_guided_flow_v7.py` | static contract `test_guided_actions_never_fail_silently` (all five call sites use the guarded helper; server side has the best-effort try/except) + `TestGuidedActionsEmailLessDriver` — 6 behavioral tests with a real email-less driver: defer succeeds (status `deferred`, seq → max+10, reason stored), resume_deferred restores seq, report_problem→exception→resume_exception, make_next → min−1, audit note posts for an email'd driver, closed stops rejected |
| `docs/DRIVER_FLOW_V7_UAT.md` | this section |
| UAT fixture (Prod-db-uat) | partner 2118 given `email=uat.driver@premafirm.com` so the walkthrough exercises the production account shape |

### Before / after

| | Before | After |
|---|---|---|
| Defer on email-less driver | 500 (`Unable to send message…`), state change **rolled back**, stop stayed `en_route` | succeeds; stop `deferred`, badge `↪ Come Back Later`, seq pushed behind serviceable stops |
| App feedback | **none** (unhandled rejection) | toast `"Could not defer the stop: <server error>"` on failure |
| Other four guided actions | same latent failure | same guard |
| Audit note for email'd driver | posts | still posts (unchanged) |

Full-suite restoration after the 2026-08-19 02:19 UAT DB upgrade

The upgrade (dispatch `18.0.3.4.0` / booking `18.0.12.0.1`) surfaced five
regressions in the suite. All fixed this session.

**Suite now 331 tests, 0 failed, 0 errors** (run 2026-08-20 03:07 UTC, after
the defer-defect work added 7 tests: `TestGuidedActionsEmailLessDriver` (6
behavioral) + the `test_guided_actions_never_fail_silently` static contract).
One intermediate full-suite run (03:02 UTC) failed exactly one test:
`test_out_of_sequence_change_requires_reason_and_is_audited` still asserted
the pre-fix call shape `action: "make_next"` — stale after the call was
re-wired through `guidedStatusCall(stop, "make_next", …)`; assertion updated,
suite green.

| Regression | Root cause | Fix |
|---|---|---|
| 161× `NotNullViolation operation_role` | Odoo 18 `required=True` → DB `NOT NULL`; in `--workers 0` inline test mode `prema_dispatch`'s tests load before the booking extension's field exists, so no default is applied at INSERT | DB column default `'custom'` on Prod-db-uat (constraint kept) |
| `TestWeeklyPlanner.test_09` | day-of-week drift: on Wed/Thu/Fri the Friday occurrence falls inside `generate_days_before=2`, making the card "due" | plan anchored to next week + same-day corridor departure |
| `TestDriverWorkday` 14/15 | hardcoded "today" (`2026-08-18`) stale once the real date passed it | `work_date` derived from America/Toronto now in `setUp` |
| `TestDispatchCrossDockCustody` transfer payload | `fields.Datetime.now()` at ~02:00 UTC → Toronto date ≠ `date.today()` → payload stops empty (StopIteration) | schedule at NOON UTC (same calendar date in Toronto at any hour) |
| `TestSavedLocationDuplicate` rule-11 | fixture used the REAL United Dairy Brampton address — collided with live UAT data (RULE 3 blocks the create) | fictional address, keeping the Blvd/Boulevard contrast |

## Walkthrough (Phase B) — results

Headless Chromium 390×844, Prod-db-uat on 8075, logged in as
`uat.driver@premafirm.com`, **2026-08-20 ~03:10–03:16 UTC** (post-fix run,
commit state of this report). Fixture reset before the run: stop 1045 back to
`arrived` (departure/confirmation cleared), pallet-photo + POP evidence
removed, partner 2118 given an email (production account shape).

**Full route walked start to finish — every stop of DISP/2026/00449 reached a
terminal state (all `completed`), no hang, no storm.**

| Step | Result | Evidence |
|---|---|---|
| Home → route loads | ✓ 8 stop rows | login 0.6s, app ready 2.5s |
| Start Route (home) | ✓ (no-op: route already started) | 0 buttons found — expected |
| Open arrived pickup (1045, storm repro state) | ✓ | stopR=1, one render |
| Guided Pickup Steps 1–6 | ✓ all six | step-2 count saved → step-3 positions (pre-assigned) → step-4 4 pallet photos (`success=True`, ISO-8601 fix holds) → step-5 POP → step-6 confirm |
| Confirm Pickup | ✓ | pickup-confirmed; 1045 → completed |
| Google Maps handoff | ✓ | `__handoff=1`, `APP.openExternalNav` override counted |
| Come Back Later (defer 1047) | ✓ **works now** | **before the fix this 500'd and failed silently**; now the stop defers, `Return to This Stop` renders |
| Return to This Stop | ✓ | stopR=3, resume → pending |
| Exception on 1048 | ✓ | **visible toast** "Problem reported — this stop remains open." (was silent failure); audit note posted (`Driver reported a stop exception … Customer Closed`) |
| Resume exception | ✓ | audit note posted (`Driver resumed stop … after resolving its exception`) |
| Delivery on 1048 (I'm Here → guide) | ✓ | "Marked as Arrived ✓", Delivery Step 1/3 unload check |
| POD (1048) | ✓ | pod evidence uploaded |
| Confirm Delivery (1048) | ✓ | 1048 → completed |
| Delivery on NOFRILLS 1047 (deferred→returned) | ✓ | arrive → unload → POD → confirm; 1047 → completed |
| Route completion | ✓ | "✓ All customer stops complete"; job auto-completed ("All stops completed and POD received"); final-schedule sampled; 1046 already completed |
| **tabStops hang (blocked the 02:16 run)** | **did NOT reproduce** | script flowed through the defer→return→tabStops sequence it hung on before |

### Console

Zero `pageerror`s and zero non-WS console errors across the whole run. The
only console noise: the known `ws://127.0.0.1:8075/websocket` 500 handshake
retries (environment finding #3 — frontend defaults the WS port to the page
port; the evented port is 8078). All 33 samples had `toast`/`kicker`/
`guideOpen` in the expected state at every waypoint.

### Latency during the walkthrough

`latAvg` held at ~16.7–16.8ms for the entire run. The `latMax` growth
(118.8ms at stop-open → 199.9ms at pre-arrival → 611.5ms at the exception
click) is one-time cost of real interactions (list re-render + toast + audit
poll per click), with the count of >100ms gaps growing **only on
interaction samples, never during idle windows** — the flat idle rows in the
storm table above are the proof the loop is gone.

### Terminal state (Prod-db-uat, post-run)

All four job-449 stops `completed`; evidence rows: 4 `popp` (pallet photos),
1 `pop_general` (pickup POP), 2 `pod_general` (McDonough + NOFRILLS). Job
audit trail: defer/resume/exception notes + auto-complete note present —
`message_post` no longer blocks driver actions.

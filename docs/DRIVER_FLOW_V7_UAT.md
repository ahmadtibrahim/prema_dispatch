# Driver Flow V7 — UAT Report (guided flow + mutation-storm fix)

- **Branch**: `feature/driver-guided-flow-v7` (PR #7 — **NOT merged, NOT deployed**)
- **Environment**: Prod-db-uat on `127.0.0.1:8075` (never Prod-db / 8069)
- **Date**: 2026-08-19
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
| AFTER — load plan open | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD |

**Result: fully resolved.** With the guide open on the arrived pickup — the
exact 90.7s scenario — the DOM is completely silent while idle: zero
mutations, zero renders, flat audit count. All render counters moved exactly
once per real interaction (1 stop render on open, 1 guide render on open, 0
load-plan writes). The remaining latency spikes (102ms initial load, 1.4s at
guide-open) are one-time layout/font/load-plan-fetch cost, not an escalating
loop: the bad-gap count did not grow during the idle window.

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

## Walkthrough (Phase B) — results

TBD
